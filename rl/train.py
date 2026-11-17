"""Train the policy to copy the bot, and measure how often it agrees on held-out games.

    .venv-rl\\Scripts\\python.exe -m rl.train --games 1000 --name g1000
    .venv-rl\\Scripts\\python.exe -m rl.train --name all            (every training game)

500 games are held out by seed (``rl/data/heldout_seeds.txt``, written once) and
never trained on; they are also the games the model later plays in ``rl.evaluate``.
Agreement is counted per head and only on decisions that had more than one legal
option, so forced moves do not flatter it.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import sys
import time
from pathlib import Path

import numpy as np
import torch
import torch.nn.functional as F

from jackdaw.env.action_space import ACTION_VERSION, NUM_ACTION_TYPES, ActionType

from rl import dataset
from rl.model import PLAY, Policy, Prepared, _TYPE_LIST, item_tokens, set_groups

DATA = Path(__file__).resolve().parent / "data"
HELDOUT = DATA / "heldout_seeds.txt"
N_HELDOUT = 500


def seed_rank(seed: str) -> str:
    """A fixed shuffle of seeds: games are held out, and added to training, in this order."""
    return hashlib.sha1(seed.encode()).hexdigest()


def heldout_seeds(seeds) -> set[str]:
    """The held-out seeds. The first call picks them from the games on hand and writes
    the list; later calls read it, so games converted afterwards all go to training."""
    if not HELDOUT.exists():
        chosen = sorted(set(seeds), key=seed_rank)[:N_HELDOUT]
        HELDOUT.write_text("\n".join(chosen) + "\n")
    return {s for s in HELDOUT.read_text().split() if s}


def split(ex: dataset.Examples, n_games: int = 0):
    """Example numbers for training (the first ``n_games`` training games, all when 0)
    and for the held-out games."""
    seeds = [str(s) for s in ex["g_seed"]]
    held = heldout_seeds(seeds)
    train_games = sorted((g for g, s in enumerate(seeds) if s not in held),
                         key=lambda g: seed_rank(seeds[g]))
    if n_games:
        train_games = train_games[:n_games]
    in_train = np.zeros(ex.n_games, bool)
    in_train[train_games] = True
    in_held = np.array([s in held for s in seeds])
    usable = (ex["flags"] & dataset.CAPPED) == 0
    game = ex["game"]
    return np.flatnonzero(in_train[game] & usable), np.flatnonzero(in_held[game] & usable), len(train_games)


def heads(model: Policy, prep: Prepared, idx: np.ndarray) -> dict:
    """Every head's scores for a batch, with the bot's choice as the label. The item and
    card heads are asked about the bot's own action type and item."""
    t = prep.tokens(idx)
    run, tokens = model.encode(t)
    types = prep.act_type[idx]
    out = {"type": (model.type_logits(run, t["type_mask"]), torch.from_numpy(types)),
           "type_mask": t["type_mask"], "value": model.value(run)}
    entities = prep.act_entity[idx]
    position = np.full(len(idx), -1, np.int64)
    rows = np.flatnonzero((_TYPE_LIST[types] >= 0) & (entities >= 0))
    if len(rows):
        mask = torch.from_numpy(prep.entity_mask(idx[rows], types[rows], tokens.shape[1]))
        position[rows] = prep.entity_base(idx[rows], types[rows]) + entities[rows]
        logits = model.entity_logits(run[rows], tokens[rows], torch.from_numpy(types[rows]), mask)
        out["item"] = (rows, logits, torch.from_numpy(position[rows]), mask)
    out["sets"] = []
    chosen = np.flatnonzero(prep.act_cards[idx] != 0)
    for group, subsets, legal in set_groups(prep, idx, chosen, types, entities):
        n = subsets.member.shape[1]
        legal_t = torch.from_numpy(legal)
        logits = model.set_logits(
            run[group], tokens[group, :n], torch.from_numpy(types[group]),
            item_tokens(tokens, group, position[group]), subsets, legal_t,
        )
        label = np.array([subsets.index[int(b)] for b in prep.act_cards[idx[group]]])
        if not legal[np.arange(len(group)), label].all():
            raise ValueError("a stored card selection is not among the legal ones")
        out["sets"].append((group, logits, torch.from_numpy(label), legal_t))
    return out


def loss_of(out: dict) -> torch.Tensor:
    loss = F.cross_entropy(*out["type"])
    if "item" in out:
        _, logits, label, _ = out["item"]
        loss = loss + F.cross_entropy(logits, label)
    if out["sets"]:
        total = sum(len(g) for g, *_ in out["sets"])
        loss = loss + sum(
            F.cross_entropy(logits, label, reduction="sum") for _, logits, label, _ in out["sets"]
        ) / total
    return loss


class Tally:
    """Counts of decisions and of agreement, added up over batches."""

    def __init__(self) -> None:
        self.n: dict[str, float] = {}

    def add(self, key: str, value) -> None:
        self.n[key] = self.n.get(key, 0.0) + float(value)

    def rate(self, hit: str, of: str) -> float:
        return self.n.get(hit, 0.0) / self.n[of] if self.n.get(of) else float("nan")


@torch.no_grad()
def measure(model: Policy, prep: Prepared, idx: np.ndarray, prior: np.ndarray,
            batch: int = 2048) -> Tally:
    """Agreement with the bot on the examples ``idx``. ``prior`` is how often each
    action type occurs in training, for the "most common legal type" baseline."""
    model.eval()
    tally = Tally()
    prior_t = torch.from_numpy(prior)
    for at in range(0, len(idx), batch):
        b = idx[at:at + batch]
        out = heads(model, prep, b)
        logits, label = out["type"]
        right = logits.argmax(-1) == label
        open_ = out["type_mask"].sum(-1) > 1
        tally.add("loss_sum", float(loss_of(out)) * len(b))
        tally.add("examples", len(b))
        tally.add("type_open", open_.sum())
        tally.add("type_right", (right & open_).sum())
        common = prior_t.masked_fill(~out["type_mask"], -1).argmax(-1)
        tally.add("type_common_right", ((common == label) & open_).sum())
        whole = right.clone()
        if "item" in out:
            rows, logits, label, mask = out["item"]
            right_i = logits.argmax(-1) == label
            open_i = mask.sum(-1) > 1
            tally.add("item_open", open_i.sum())
            tally.add("item_right", (right_i & open_i).sum())
            tally.add("item_first_right", ((mask.float().argmax(-1) == label) & open_i).sum())
            whole[rows] &= right_i
        for rows, logits, label, legal in out["sets"]:
            right_s = logits.argmax(-1) == label
            open_s = legal.sum(-1) > 1
            tally.add("cards_open", open_s.sum())
            tally.add("cards_right", (right_s & open_s).sum())
            whole[rows] &= right_s
            plays = prep.act_type[b[rows]] == PLAY
            if plays.any():
                r = b[rows[plays]]
                tally.add("play_open", (open_s.numpy() & plays).sum())
                tally.add("play_best_hand_right",
                          ((prep.best_bits[r] == prep.act_cards[r]) & open_s.numpy()[plays]).sum())
        tally.add("whole_right", whole.sum())
        types = prep.act_type[b]
        whole_np = whole.numpy()
        for t in np.unique(types):
            tally.add(f"n_{t}", (types == t).sum())
            tally.add(f"whole_{t}", whole_np[types == t].sum())
    return tally


def report(tally: Tally) -> dict:
    out = {
        "held-out examples": int(tally.n["examples"]),
        "loss": tally.n["loss_sum"] / tally.n["examples"],
        "action type": tally.rate("type_right", "type_open"),
        "action type, most common legal type": tally.rate("type_common_right", "type_open"),
        "item": tally.rate("item_right", "item_open"),
        "item, first legal item": tally.rate("item_first_right", "item_open"),
        "cards": tally.rate("cards_right", "cards_open"),
        "plays, cards marked best hand": tally.rate("play_best_hand_right", "play_open"),
        "whole action": tally.rate("whole_right", "examples"),
        "decisions": {k: int(tally.n.get(k, 0)) for k in ("type_open", "item_open", "cards_open")},
        "by action type": {
            ActionType(t).name: {
                "examples": int(tally.n[f"n_{t}"]),
                "whole action": tally.n[f"whole_{t}"] / tally.n[f"n_{t}"],
            }
            for t in range(NUM_ACTION_TYPES) if tally.n.get(f"n_{t}")
        },
    }
    return out


def save_model(path: Path, model: Policy, config: dict, meta: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    torch.save({
        "state": model.state_dict(), "config": config, "meta": meta,
        "jackdaw": importlib.metadata.version("jackdaw"), "action_version": ACTION_VERSION,
    }, path)


def load_model(path: str | Path) -> tuple[Policy, dict]:
    """A saved policy, checked against the simulator and action space installed here."""
    saved = torch.load(path, weights_only=False)
    here = (importlib.metadata.version("jackdaw"), ACTION_VERSION)
    if (saved["jackdaw"], saved["action_version"]) != here:
        raise ValueError(f"{path} was trained on jackdaw {saved['jackdaw']} action version "
                         f"{saved['action_version']}; this is {here}")
    model = Policy(**saved["config"])
    # A policy trained only to copy the bot has no value head; it starts untrained.
    missing, unexpected = model.load_state_dict(saved["state"], strict=False)
    if unexpected or any(not k.startswith("value_head.") for k in missing):
        raise ValueError(f"{path} does not fit this network: missing {missing}, "
                         f"unexpected {unexpected}")
    model.config = dict(saved["config"])
    model.eval()
    return model, saved["meta"]


def say(line: str) -> None:
    print(line, flush=True)


def train(prep: Prepared, train_idx, held_idx, *, steps: int, batch: int, lr: float,
          config: dict, seed: int = 0, seconds: float = 0.0, log=say, checkpoint=None):
    """Train a new policy; returns it with the held-out measurements taken along the way.
    ``checkpoint``, when given, is called with the policy and the step at every
    measurement, so a long run that is cut off leaves its latest state behind."""
    torch.manual_seed(seed)
    rng = np.random.default_rng(seed)
    model = Policy(**config)
    opt = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=0.01)
    warmup = min(200, steps // 10)

    def rate(step: int) -> float:
        if step < warmup:
            return (step + 1) / warmup
        return 0.5 * (1 + math.cos(math.pi * (step - warmup) / max(1, steps - warmup)))

    schedule = torch.optim.lr_scheduler.LambdaLR(opt, rate)
    prior = np.bincount(prep.act_type[train_idx], minlength=NUM_ACTION_TYPES).astype(np.float32)
    probe = held_idx if len(held_idx) <= 20_000 else rng.choice(held_idx, 20_000, replace=False)
    history = []
    order = rng.permutation(train_idx)
    at = 0
    started = time.perf_counter()
    running = 0.0
    for step in range(steps):
        if at + batch > len(order):
            order, at = rng.permutation(train_idx), 0
        b = np.sort(order[at:at + batch])
        at += batch
        model.train()
        loss = loss_of(heads(model, prep, b))
        opt.zero_grad(set_to_none=True)
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        opt.step()
        schedule.step()
        running = loss.item() if step == 0 else 0.98 * running + 0.02 * loss.item()
        done = step + 1
        elapsed = time.perf_counter() - started
        if done % 500 == 0 or done == steps or (seconds and elapsed > seconds):
            r = report(measure(model, prep, probe, prior))
            history.append({"step": done, "train loss": running, "held-out loss": r["loss"],
                            "whole action": r["whole action"]})
            log(f"step {done:>6}  {elapsed:6.0f}s  train loss {running:.3f}  held-out loss "
                f"{r['loss']:.3f}  type {r['action type']:.3f}  item {r['item']:.3f}  "
                f"cards {r['cards']:.3f}  whole {r['whole action']:.3f}")
            if checkpoint is not None:
                checkpoint(model, done)
            if seconds and elapsed > seconds:
                break
    return model, prior, history


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--shards", default=str(DATA / "shards"))
    ap.add_argument("--games", type=int, default=0, help="training games to use (0: all)")
    ap.add_argument("--epochs", type=float, default=10.0)
    ap.add_argument("--batch", type=int, default=512)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--width", type=int, default=128)
    ap.add_argument("--layers", type=int, default=2)
    ap.add_argument("--seconds", type=float, default=0.0, help="stop after this long (timing runs)")
    ap.add_argument("--name", default="", help="save the model as rl/data/models/<name>.pt")
    args = ap.parse_args()

    started = time.perf_counter()
    ex = dataset.load_shards(sorted(Path(args.shards).glob("shard_*.npz")))
    train_idx, held_idx, n_games = split(ex, args.games)
    prep = Prepared(ex)
    steps = max(1, int(args.epochs * len(train_idx) / args.batch))
    say(f"{ex.n_games} games loaded in {time.perf_counter() - started:.1f}s: {n_games} training "
        f"games ({len(train_idx):,} examples), {len(held_idx):,} held-out examples; "
        f"{steps} steps of {args.batch}")
    config = {"width": args.width, "layers": args.layers}
    out = DATA / "models"

    def checkpoint(model: Policy, step: int) -> None:
        if args.name:
            save_model(out / f"{args.name}.pt", model, config,
                       {"training games": n_games, "steps": step})

    model, prior, history = train(prep, train_idx, held_idx, steps=steps, batch=args.batch,
                                  lr=args.lr, config=config, seconds=args.seconds,
                                  checkpoint=checkpoint)
    result = report(measure(model, prep, held_idx, prior))
    result["training games"] = n_games
    result["training examples"] = int(len(train_idx))
    result["history"] = history
    print(json.dumps({k: v for k, v in result.items() if k != "history"}, indent=2))
    if args.name:
        save_model(out / f"{args.name}.pt", model, config,
                   {"training games": n_games, "steps": history[-1]["step"] if history else 0})
        (out / f"{args.name}.json").write_text(json.dumps(result, indent=2))
        print(f"saved {out / (args.name + '.pt')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
