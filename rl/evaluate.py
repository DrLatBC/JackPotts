"""Let a trained policy play the held-out seeds in Jackdaw, next to the bot's own results.

    .venv-rl\\Scripts\\python.exe -m rl.evaluate --name all

The policy plays each held-out seed through ``jackdaw.env.BalatroEnvironment`` on the
deck and stake the bot played it on, always taking its most likely legal action. An
episode ends at a win, so a win counts as ante 9 for the policy and for the bot (which
plays on into endless mode). The bot's result for the same seed comes from its replay.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from dataclasses import replace
from pathlib import Path

import torch
from jackdaw.core.game import IllegalActionError
from jackdaw.env.action_space import ActionType

from rl import dataset
from rl.model import Policy, Prepared, choose
from rl.ordering import put_in_order
from rl.playback import env_view, make_env, no_legal_action, read_seeds
from rl.train import HELDOUT, load_model

DATA = Path(__file__).resolve().parent / "data"
WIN_ANTE = 9
MAX_STEPS = 3000
PAST = (1, 2, 4, 6)
DECK, STAKE = "b_blue", 1
"""What learning by playing plays on: Blue deck, White stake."""


def eval_seed(n: int) -> str:
    """The ``n``-th evaluation seed (from 1). The dash keeps these apart from the game's
    own 8-character seeds and from training seeds."""
    return f"RLE-{n:06d}"


def play(model: Policy, games: list[tuple[str, str, int]], *, parallel: int = 64,
         max_steps: int = MAX_STEPS, order_plays: bool = True) -> list[dict]:
    """Play each ``(seed, deck, stake)`` to its end. Per game: whether it was won, the
    final ante (9 for a win), the steps taken, and why it was cut short, if it was.
    ``order_plays`` plays the chosen cards in the bot's left-to-right order
    (``rl.ordering``); without it they score in hand order."""
    results: list[dict] = []
    waiting = list(reversed(games))
    active: list[dict] = []
    while waiting or active:
        while waiting and len(active) < parallel:
            seed, deck, stake = waiting.pop()
            env = make_env(deck, stake, max_steps=max_steps)
            _, _, info = env.reset(seed=seed)
            active.append({"seed": seed, "env": env, "info": info, "steps": 0})
        prep = Prepared(dataset.examples_of([env_view(slot["info"]) for slot in active]))
        actions = choose(model, prep)
        still = []
        for slot, action in zip(active, actions):
            cut = None
            if order_plays and action.action_type == ActionType.PlayHand:
                action = replace(
                    action, card_target=put_in_order(slot["info"]["game"], action.card_target))
            try:
                _, terminated, truncated, _, info = slot["env"].step(action)
            except IllegalActionError as e:
                terminated, truncated, info = False, True, slot["info"]
                cut = f"refused: {e}"
            if not (terminated or truncated) and no_legal_action(info):
                truncated, cut = True, "no legal action"
            slot["info"] = info
            slot["steps"] += 1
            if not (terminated or truncated):
                still.append(slot)
                continue
            state = info["state"]
            if truncated and cut is None:
                cut = ("not ported: " + str(info["not_ported"]) if "not_ported" in info
                       else "step limit")
            won = bool(state.won) and cut is None
            results.append({
                "seed": slot["seed"], "won": won,
                "ante": WIN_ANTE if won else min(int(state.ante), WIN_ANTE),
                "steps": slot["steps"], "cut": cut,
            })
        active = still
    return results


def summary(rows: list[dict]) -> dict:
    """Win rate, mean final ante and how far the games got."""
    n = len(rows)
    wins = sum(r["won"] for r in rows)
    rate = wins / n
    return {
        "games": n,
        "wins": wins,
        "win rate": rate,
        "win rate +/-": math.sqrt(rate * (1 - rate) / n),
        "mean final ante": sum(r["ante"] for r in rows) / n,
        **{f"past ante {k}": sum(r["ante"] > k for r in rows) / n for k in PAST},
    }


def bot_results(ex: dataset.Examples, seeds: set[str]) -> dict[str, dict]:
    """The bot's own result on each of the seeds, from the replays the shards came from."""
    out = {}
    for g in range(ex.n_games):
        seed = str(ex["g_seed"][g])
        if seed in seeds:
            won = bool(ex["g_won"][g])
            out[seed] = {
                "seed": seed, "won": won,
                "ante": WIN_ANTE if won else min(int(ex["g_final"][g][0]), WIN_ANTE),
                "deck": str(ex["g_deck"][g]), "stake": int(ex["g_stake"][g]),
            }
    return out


def table(model_rows: list[dict], bot_rows: list[dict]) -> str:
    m, b = summary(model_rows), summary(bot_rows)
    lines = [f"{'':24}{'model':>10}{'bot':>10}",
             f"{'games':24}{m['games']:>10}{b['games']:>10}"]
    for key in ("win rate", "mean final ante", *(f"past ante {k}" for k in PAST)):
        if key == "mean final ante":
            lines.append(f"{key:24}{m[key]:>10.2f}{b[key]:>10.2f}")
        else:
            lines.append(f"{key:24}{m[key]:>9.1%} {b[key]:>9.1%}")
    lines.append(f"{'win rate +/- (1 s.e.)':24}{m['win rate +/-']:>9.1%} {b['win rate +/-']:>9.1%}")
    return "\n".join(lines)


def evaluation_seeds(model: Policy, args, path: Path) -> int:
    """Play the evaluation seeds ``RLE-000001...``; results saved beside the model."""
    seeds = [eval_seed(i) for i in range(1, args.seeds + 1)]
    started = time.perf_counter()
    rows = play(model, [(s, DECK, STAKE) for s in seeds], parallel=args.parallel,
                order_plays=not args.hand_order)
    s = summary(rows)
    s["seconds"] = time.perf_counter() - started
    s["steps"] = sum(r["steps"] for r in rows)
    print(json.dumps(s, indent=2))
    out = path.with_name(f"{path.stem}_eval{args.seeds}.json")
    out.write_text(json.dumps({"model": str(path), "summary": s, "games": rows}, indent=2))
    print(f"saved {out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--name", required=True,
                    help="the model: rl/data/models/<name>.pt, or a path to a .pt file")
    ap.add_argument("--shards", default=str(DATA / "shards"))
    ap.add_argument("--games", type=int, default=0, help="play only this many seeds")
    ap.add_argument("--parallel", type=int, default=64)
    ap.add_argument("--threads", type=int, default=0, help="Torch threads (0: Torch's default)")
    ap.add_argument("--seeds", type=int, default=0,
                    help="play the first N evaluation seeds (Blue deck, White stake) instead "
                         "of the held-out seeds; the bot has no results on them")
    ap.add_argument("--hand-order", action="store_true",
                    help="play the chosen cards in hand order, not the bot's play order")
    args = ap.parse_args()
    if args.threads:
        torch.set_num_threads(args.threads)

    path = Path(args.name) if args.name.endswith(".pt") else DATA / "models" / f"{args.name}.pt"
    model, meta = load_model(path)
    if args.seeds:
        return evaluation_seeds(model, args, path)
    ex = dataset.load_shards(sorted(Path(args.shards).glob("shard_*.npz")))
    bot = bot_results(ex, read_seeds(HELDOUT))
    seeds = sorted(bot)[:args.games or None]
    started = time.perf_counter()
    rows = play(model, [(s, bot[s]["deck"], bot[s]["stake"]) for s in seeds],
                parallel=args.parallel, order_plays=not args.hand_order)
    rows.sort(key=lambda r: r["seed"])
    bot_rows = [bot[s] for s in seeds]
    steps = sum(r["steps"] for r in rows)
    how = "hand order" if args.hand_order else "the bot's play order"
    print(f"model {args.name} (trained on {meta['training games']} games) played {len(rows)} "
          f"held-out seeds in {how}, {steps:,} steps, in {time.perf_counter() - started:.0f}s\n")
    print(table(rows, bot_rows))
    cuts: dict[str, int] = {}
    for r in rows:
        if r["cut"]:
            cause = r["cut"].split(":")[0]
            cuts[cause] = cuts.get(cause, 0) + 1
    print("\ngames cut short:", cuts or "none")
    for r in rows:
        if r["cut"]:
            print(f"  seed {r['seed']} at ante {r['ante']} step {r['steps']}: {r['cut']}")
    both = sum(r["won"] and b["won"] for r, b in zip(rows, bot_rows))
    print(f"\nseeds won by both: {both}; by the model only: "
          f"{sum(r['won'] for r in rows) - both}; by the bot only: "
          f"{sum(b['won'] for b in bot_rows) - both}")
    out = {"model": args.name, "meta": meta, "play order": how, "model summary": summary(rows),
           "bot summary": summary(bot_rows), "cut short": cuts,
           "games": [{**r, "bot ante": b["ante"], "bot won": b["won"]}
                     for r, b in zip(rows, bot_rows)]}
    saved = path.with_name(f"{path.stem}_eval{'_hand_order' if args.hand_order else ''}.json")
    saved.write_text(json.dumps(out, indent=2))
    print(f"saved {saved}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
