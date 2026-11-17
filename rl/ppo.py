"""Learning by playing: PPO from the copy of the bot.

    .venv-rl\\Scripts\\python.exe -m rl.ppo train --start b187 --run r1 --hours 8

``train`` plays and learns in turns. By default (``--plays 4``) each update plays
``--seeds`` new seeds 4 times each, to their end; a game's moves are judged by how the
game did against the other plays of its seed, which cancels most of the seed's luck.
With ``--plays 1`` moves are judged by the value head instead (every game slot plays
``--ticks`` steps per update), after ``fit-value`` has fitted that head alone on games
of the copy. Either way the policy then learns for ``--passes`` passes; or, with
``--games-per-step``, takes one step on the gradient of that many games, sized by how far
it moves the policy (``one_step``).

Each step's points (``rl.selfplay.points``) are reduced by the drift penalty: ``beta``
times how much more likely the playing policy found its move than the copy does
(log-probabilities). ``beta`` falls linearly to zero over the first ``--fade`` share
of the run. Everything is written under ``rl/data/ppo/<run>/``.

``train`` also keeps ``bot_log/training/<run>.json`` current for the control panel,
stops between updates (saved, ready for ``--resume``) once ``bot_log/training/<run>.stop``
appears, and refuses to start a run another live process is playing.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import sys
import time
from dataclasses import replace
from pathlib import Path

import numpy as np
import torch
import torch.nn.functional as F

from rl.evaluate import DECK, STAKE, eval_seed, play, summary
from rl.model import VALUE_SCALE, Policy, Prepared, act
from rl.selfplay import SelfPlay, advantages, same_seed_scores
from rl.train import heads, load_model, save_model

from balatro_bot.files import replace_file  # noqa: E402  (src is on the path once rl is imported)

DATA = Path(__file__).resolve().parent / "data"
PPO = DATA / "ppo"
BELOW_NORMAL = 0x4000
# The control panel's folder (balatro_bot's default log_dir); balatro_bot isn't in .venv-rl.
TRAINING = Path(__file__).resolve().parent.parent / "bot_log" / "training"


def below_normal_priority() -> None:
    """Let everything else on the PC go first (Windows)."""
    if sys.platform == "win32":
        kernel = ctypes.windll.kernel32
        kernel.SetPriorityClass(kernel.GetCurrentProcess(), BELOW_NORMAL)


def scored(model: Policy, prep: Prepared, idx: np.ndarray):
    """Per example: the log-probability of its stored action, the entropy of the
    choices that action went through, and the value."""
    out = heads(model, prep, idx)
    logits, label = out["type"]
    logp = torch.log_softmax(logits, -1).gather(-1, label[:, None]).squeeze(-1)
    ent = _entropy(logits)
    if "item" in out:
        rows, logits, label, _ = out["item"]
        rows = torch.as_tensor(rows)
        logp = logp.index_add(0, rows, torch.log_softmax(logits, -1).gather(-1, label[:, None]).squeeze(-1))
        ent = ent.index_add(0, rows, _entropy(logits))
    for rows, logits, label, _ in out["sets"]:
        rows = torch.as_tensor(rows)
        logp = logp.index_add(0, rows, torch.log_softmax(logits, -1).gather(-1, label[:, None]).squeeze(-1))
        ent = ent.index_add(0, rows, _entropy(logits))
    return logp, ent, out["value"]


def _entropy(logits: torch.Tensor) -> torch.Tensor:
    # Illegal choices have -inf logits; their log-probability is replaced before the
    # product so neither the value nor its gradient meets 0 x inf.
    logp = torch.log_softmax(logits, -1)
    safe = torch.where(torch.isfinite(logp), logp, torch.zeros_like(logp))
    return -(safe.exp() * safe).masked_fill(~torch.isfinite(logp), 0.0).sum(-1)


@torch.no_grad()
def copy_logp(copy: Policy, prep: Prepared, batch: int = 2048) -> np.ndarray:
    """How likely the copy finds each stored action."""
    out = np.zeros(prep.n, np.float32)
    for at in range(0, prep.n, batch):
        idx = np.arange(at, min(at + batch, prep.n))
        out[idx] = scored(copy, prep, idx)[0].numpy()
    return out


@torch.no_grad()
def agreement(model: Policy, copy: Policy, prep: Prepared, n: int, rng) -> float:
    """On ``n`` of the steps, how often the model's most likely action is the copy's."""
    idx = np.sort(rng.choice(prep.n, min(n, prep.n), replace=False))
    a, b = act(model, prep, idx)[0], act(copy, prep, idx)[0]
    return float(np.mean([x == y for x, y in zip(a, b)]))


def explained(target: np.ndarray, guess: np.ndarray) -> float:
    """1 when the guess is exact, 0 when it is no better than the average."""
    var = float(np.var(target))
    return float("nan") if var == 0 else 1.0 - float(np.var(target - guess)) / var


def seed_share(points: np.ndarray) -> float:
    """Of the spread in points between games, the share that comes with the seed
    (rows: seeds, columns: plays of it). 0 when the seed tells nothing, 1 when every
    play of a seed scores the same."""
    within = float(points.var(1, ddof=1).mean())
    between = float(points.mean(1).var(ddof=1)) - within / points.shape[1]
    total = between + within
    return float("nan") if total <= 0 else max(between, 0.0) / total


def replace_ns(args: argparse.Namespace, **changes) -> argparse.Namespace:
    return argparse.Namespace(**{**vars(args), **changes})


def write_json(path: Path, obj) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=2))
    if not replace_file(tmp, path):
        raise PermissionError(f"{path} stayed open elsewhere; the new copy is in {tmp}")


# -- status for the control panel ------------------------------------------------


def status_path(run: str) -> Path:
    return TRAINING / f"{run}.json"


def stop_path(run: str) -> Path:
    return TRAINING / f"{run}.stop"


def report(status: dict, **changes) -> None:
    """Merge the changes into the run's status and write it out; a write that doesn't get
    through is left for the next one (the status only feeds the control panel)."""
    status.update(changes, pid=os.getpid(), updated=time.time())
    try:
        TRAINING.mkdir(parents=True, exist_ok=True)
        write_json(status_path(status["run"]), status)
    except OSError:
        pass


def pid_alive(pid: int) -> bool:
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    query_limited_information, still_active = 0x1000, 259
    kernel = ctypes.windll.kernel32
    handle = kernel.OpenProcess(query_limited_information, False, pid)
    if not handle:
        return False
    code = ctypes.c_ulong()
    ok = kernel.GetExitCodeProcess(handle, ctypes.byref(code))
    kernel.CloseHandle(handle)
    return bool(ok) and code.value == still_active


def read_status(run: str) -> dict:
    try:
        return json.loads(status_path(run).read_text())
    except (OSError, ValueError):
        return {}


def playing_elsewhere(run: str) -> int | None:
    """The pid of another live process playing this run, if there is one."""
    status = read_status(run)
    pid = int(status.get("pid", 0))
    if status.get("phase") == "running" and pid != os.getpid() and pid_alive(pid):
        return pid
    return None


def claim(run: str) -> dict:
    """Refuse when another process is playing this run, else mark it as ours.

    Two processes on one run folder overwrite each other's checkpoints and both append
    to its log. The status is written before the model loads to keep the window in
    which two starts can both pass this check short.
    """
    pid = playing_elsewhere(run)
    if pid is not None:
        raise SystemExit(f"{run} is already being played by process {pid}")
    stop_path(run).unlink(missing_ok=True)
    status = {"run": run, "phase": "running", "doing": "starting"}
    report(status)
    return status


def progress(state: dict, args) -> dict:
    return {"update": state["updates"], "hours": state["seconds"] / 3600,
            "hours total": args.hours, "games": state["games"]}


def latest_numbers(line: dict) -> dict:
    return {"points per game": line["points per finished game"],
            "agreement": line["agreement with copy"], "drift": line["drift"]}


def eval_numbers(summary: dict) -> dict:
    return {"eval": {k: summary[k] for k in ("update", "win rate", "win rate +/-", "mean final ante")}}


def last_numbers(log_path: Path) -> dict:
    """The latest update's numbers, for a resumed run's first status."""
    lines = log_path.read_text().splitlines() if log_path.exists() else []
    return latest_numbers(json.loads(lines[-1])) if lines else {}


def last_eval(out: Path) -> dict:
    """The run's newest evaluation, for a resumed run's first status."""
    evals = sorted(out.glob("eval_u*.json"))
    return eval_numbers(json.loads(evals[-1].read_text())["summary"]) if evals else {}


def stop_requested(run: str) -> bool:
    """True once a stop has been asked for this run; the request is used up."""
    try:
        stop_path(run).unlink()
    except FileNotFoundError:
        return False
    return True


def report_failure(run: str, error: str) -> None:
    """Mark the run failed if this process claimed it; a refusal before the claim leaves
    the status of whoever played the run last."""
    status = read_status(run)
    if status.get("pid") == os.getpid():
        report(status, phase="failed", doing="", error=error)


# -- fit-value ----------------------------------------------------------------


def fit_value(args) -> int:
    out = PPO / args.run
    out.mkdir(parents=True, exist_ok=True)
    model, meta = load_model(DATA / "models" / f"{args.start}.pt")
    started = time.perf_counter()
    played = SelfPlay(args.run + "-value", seed=args.seed).whole_games(model, args.games, args.slots)
    t_play = time.perf_counter() - started
    target = played.points_to_go()
    runs = run_vectors(model, Prepared(played.examples))
    held = (played.game % 5) == 0  # a fifth of the games, all of each game's steps
    fit_head(model, runs[~held], target[~held], steps=args.fit_steps)
    with torch.no_grad():
        guess = model.value(torch.from_numpy(runs)).numpy() * VALUE_SCALE
    result = {
        "start": args.start, "games": len(played.results), "steps": int(len(target)),
        "play seconds": t_play, "seconds": time.perf_counter() - started,
        "explained, training games": explained(target[~held], guess[~held]),
        "explained, held-back games": explained(target[held], guess[held]),
        "mean points per game": float(np.mean([g["points"] for g in played.results])),
        "sampled play": summary(played.results),
        "cut short": [g["cut"] for g in played.results if g["cut"]],
    }
    print(json.dumps(result, indent=2))
    save_model(out / "start.pt", model, model.config,
               {**meta, "copy": args.start, "value fit": result})
    write_json(out / "value_fit.json", result)
    return 0


@torch.no_grad()
def run_vectors(model: Policy, prep: Prepared, batch: int = 2048) -> np.ndarray:
    """The run token after the transformer, per example: what the value head reads."""
    out = np.zeros((prep.n, model.width), np.float32)
    for at in range(0, prep.n, batch):
        idx = np.arange(at, min(at + batch, prep.n))
        out[idx] = model.encode(prep.tokens(idx))[0].numpy()
    return out


def fit_head(model: Policy, runs: np.ndarray, target: np.ndarray, *, steps: int,
             batch: int = 1024, lr: float = 1e-3, seed: int = 0) -> None:
    """Fit the value head alone on run tokens already computed."""
    rng = np.random.default_rng(seed)
    head = model.value_head
    opt = torch.optim.Adam(head.parameters(), lr=lr)
    x, y = torch.from_numpy(runs), torch.from_numpy(target / VALUE_SCALE)
    for step in range(steps):
        b = torch.from_numpy(rng.integers(0, len(y), batch))
        loss = F.mse_loss(model.value(x[b]), y[b])
        opt.zero_grad(set_to_none=True)
        loss.backward()
        opt.step()


# -- train ----------------------------------------------------------------------


def starting_point(args, out: Path):
    """The policy a run carries on from: its latest checkpoint when resuming, else the
    value-fitted start (``fit-value``), else the copy itself."""
    if args.resume:
        return load_model(out / "latest.pt")
    if (out / "start.pt").exists():
        return load_model(out / "start.pt")
    if not args.start:
        raise SystemExit(f"no {out / 'start.pt'}: pass --start <copy> or run fit-value first")
    out.mkdir(parents=True, exist_ok=True)
    model, meta = load_model(DATA / "models" / f"{args.start}.pt")
    return model, {**meta, "copy": args.start}


def train(args) -> int:
    out = PPO / args.run
    log_path = out / "log.jsonl"
    if not args.resume and log_path.exists():
        raise SystemExit(f"{log_path} exists: pass --resume to carry on, or pick another --run")
    status = claim(args.run)
    model, meta = starting_point(args, out)
    copy, _ = load_model(DATA / "models" / f"{meta['copy']}.pt")
    copy.eval()
    for p in copy.parameters():
        p.requires_grad_(False)
    state_path = out / "state.json"
    state =json.loads(state_path.read_text()) if args.resume else {
        "updates": 0, "seconds": 0.0, "next_seed": 0, "steps": 0, "games": 0,
        "beta": args.beta, "fade_seconds": args.fade * args.hours * 3600,
        "next_eval": args.eval_every * 3600, "args": vars(args),
    }
    opt = torch.optim.Adam(model.parameters(), lr=args.lr)
    if args.resume and (out / "optim.pt").exists():
        opt.load_state_dict(torch.load(out / "optim.pt"))
    games = SelfPlay(args.run, seed=args.seed + state["updates"], first=state["next_seed"])
    rng = np.random.default_rng(args.seed + state["updates"])
    eval_seeds = [eval_seed(i) for i in range(1, args.eval_games + 1)]
    report(status, copy=meta["copy"], beta=state["beta"], doing="playing",
           **progress(state, args), **last_numbers(log_path), **last_eval(out))
    if not args.resume and args.eval_every:
        save_model(out / "latest.pt", model, model.config, {**meta, "updates": 0})
        evaluate_and_report(model, out, state, eval_seeds, status)
    began = time.perf_counter() - state["seconds"]
    learn_args = replace_ns(args, value_coef=0.0) if args.plays > 1 else args
    while state["seconds"] < args.hours * 3600:
        if stop_requested(args.run):
            report(status, phase="stopped", doing="")
            return 0
        t0 = time.perf_counter()
        beta = state["beta"] if state["fade_seconds"] <= 0 else \
            state["beta"] * max(0.0, 1.0 - state["seconds"] / state["fade_seconds"])
        if args.plays > 1:
            total = args.games_per_step or args.seeds * args.plays
            played = games.whole_games(model, total, parallel=args.seeds * args.plays,
                                       repeat=args.plays)
            t1 = time.perf_counter()
            prep = Prepared(played.examples)
            drift = played.logp - copy_logp(copy, prep)
            adv, _ = same_seed_scores(played, args.plays, beta * drift)
            ret = np.zeros(prep.n, np.float32)
            old_logp, finished = played.logp, played.results
            points = np.array([g["points"] for g in finished]).reshape(-1, args.plays)
            extra = {"seed luck share": seed_share(points)}
        else:
            r = games.collect(model, args.slots, args.ticks)
            t1 = time.perf_counter()
            prep = Prepared(r.examples)
            drift = r.logp - copy_logp(copy, prep)
            adv, ret = advantages(replace(r, reward=r.reward - beta * drift), args.lam)
            old_logp, finished = r.logp, r.finished
            extra = {"value explained": explained(ret, r.value)}
        norm = (adv - adv.mean()) / (adv.std() + 1e-8)
        if args.games_per_step:
            stats = one_step(model, opt, prep, old_logp, norm, state, args, rng)
        else:
            stats = update(model, opt, prep, old_logp, norm, ret, learn_args, rng)
        t2 = time.perf_counter()
        state["updates"] += 1
        state["seconds"] = time.perf_counter() - began
        state["next_seed"] = games.next_seed
        state["steps"] += prep.n
        state["games"] += len(finished)
        line = {
            "update": state["updates"], "hours": state["seconds"] / 3600,
            "steps": state["steps"], "games": state["games"], "beta": beta,
            "points per finished game": float(np.mean([g["points"] for g in finished]))
            if finished else None,
            "finished games": len(finished),
            "won": int(sum(g["won"] for g in finished)),
            "mean final ante": float(np.mean([g["ante"] for g in finished])) if finished else None,
            "cut short": [g["cut"] for g in finished if g["cut"]],
            "drift": float(drift.mean()),
            "agreement with copy": agreement(model, copy, prep, 1024, rng),
            "play s": t1 - t0, "learn s": t2 - t1, **extra, **stats,
        }
        with log_path.open("a") as f:
            f.write(json.dumps(line) + "\n")
        shown = " ".join(f"{k.split()[0]} {v:.2f}" for k, v in extra.items())
        moved = (f"change {stats['change']:.4f}  lr {stats['lr']:.1e}  tries {stats['tries']}"
                 if "change" in stats else f"clip {stats['clipped']:.3f}")
        print(f"u{line['update']:>4} {line['hours']:5.2f}h  games {state['games']:>7}  pts "
              f"{line['points per finished game'] or 0:5.2f}  drift {line['drift']:.3f}  "
              f"agree {line['agreement with copy']:.3f}  {shown}  "
              f"ent {stats['entropy']:.2f}  {moved}  "
              f"play {line['play s']:.0f}s learn {line['learn s']:.0f}s", flush=True)
        save_model(out / "latest.pt", model, model.config, {**meta, "updates": state["updates"]})
        torch.save(opt.state_dict(), out / "optim.pt")
        write_json(state_path, state)
        report(status, **progress(state, args), **latest_numbers(line))
        if args.eval_every and state["seconds"] >= state["next_eval"]:
            state["next_eval"] = state["seconds"] + args.eval_every * 3600
            evaluate_and_report(model, out, state, eval_seeds, status)
            write_json(state_path, state)
    evaluate_and_report(model, out, state, eval_seeds, status)
    stop_path(args.run).unlink(missing_ok=True)
    report(status, phase="finished", doing="")
    return 0


def evaluate_and_report(model: Policy, out: Path, state: dict, seeds: list[str], status: dict) -> None:
    report(status, doing="evaluating")
    report(status, doing="playing", **eval_numbers(evaluate(model, out, state, seeds)))


def update(model: Policy, opt, prep: Prepared, old_logp, adv, ret, args, rng) -> dict:
    """``args.passes`` passes of PPO over the rollout."""
    model.train()
    old = torch.from_numpy(old_logp)
    adv_t = torch.from_numpy(adv.astype(np.float32))
    target = torch.from_numpy((ret / VALUE_SCALE).astype(np.float32))
    sums = {"policy loss": 0.0, "value loss": 0.0, "entropy": 0.0, "clipped": 0.0, "ratio": 0.0}
    batches = 0
    for _ in range(args.passes):
        order = rng.permutation(prep.n)
        for at in range(0, prep.n, args.batch):
            idx = np.sort(order[at:at + args.batch])
            logp, ent, value = scored(model, prep, idx)
            t = torch.from_numpy(idx)
            ratio = torch.exp(logp - old[t])
            a = adv_t[t]
            policy = -torch.minimum(ratio * a, ratio.clamp(1 - args.clip, 1 + args.clip) * a).mean()
            value_loss = F.mse_loss(value, target[t])
            loss = policy + args.value_coef * value_loss - args.entropy_coef * ent.mean()
            opt.zero_grad(set_to_none=True)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), args.max_grad)
            opt.step()
            batches += 1
            with torch.no_grad():
                sums["policy loss"] += policy.item()
                sums["value loss"] += value_loss.item()
                sums["entropy"] += ent.mean().item()
                sums["clipped"] += ((ratio - 1).abs() > args.clip).float().mean().item()
                sums["ratio"] += ratio.mean().item()
    model.eval()
    return {k: v / batches for k, v in sums.items()}


def one_step(model: Policy, opt, prep: Prepared, old_logp, adv, state: dict, args, rng,
             tries: int = 5) -> dict:
    """One optimiser step on the policy gradient of the whole batch, sized so the policy
    moves by about ``args.target_change`` on the batch's own moves.

    The change is the mean of (r - 1) - log r over a sample of the stored moves, r being
    new over old probability: an estimate of KL(old || new) that is never negative. A
    step that moves more than twice the target is undone and retried at half the
    learning rate; the next step's rate is scaled by sqrt(target / change), within
    0.5x to 2x, since the change grows with the square of the step."""
    model.train()
    adv_t = torch.from_numpy(adv.astype(np.float32))
    opt.zero_grad(set_to_none=True)
    loss_sum = 0.0
    for at in range(0, prep.n, args.batch):
        idx = np.arange(at, min(at + args.batch, prep.n))
        logp, _, _ = scored(model, prep, idx)
        loss = -(adv_t[torch.from_numpy(idx)] * logp).sum() / prep.n
        loss.backward()
        loss_sum += loss.item()
    grad_norm = float(torch.nn.utils.clip_grad_norm_(model.parameters(), args.max_grad))
    model.eval()

    sample = np.sort(rng.choice(prep.n, min(args.change_sample, prep.n), replace=False))
    old = torch.from_numpy(old_logp[sample])
    saved_model = {k: v.clone() for k, v in model.state_dict().items()}
    saved_opt = copy_state(opt.state_dict())
    lr = state.setdefault("step lr", args.lr)
    for attempt in range(1, tries + 1):
        for group in opt.param_groups:
            group["lr"] = lr
        opt.step()
        with torch.no_grad():
            new, ent, _ = scored(model, prep, sample)
            log_r = new - old
            change = float(((log_r.exp() - 1) - log_r).mean())
        if change <= 2 * args.target_change or attempt == tries:
            break
        model.load_state_dict(saved_model)
        opt.load_state_dict(copy_state(saved_opt))
        lr *= 0.5
    state["step lr"] = lr * float(np.clip(np.sqrt(args.target_change / max(change, 1e-12)), 0.5, 2.0))
    return {"policy loss": loss_sum, "entropy": float(ent.mean()), "change": change,
            "lr": lr, "tries": attempt, "grad norm": grad_norm}


def copy_state(d):
    if isinstance(d, torch.Tensor):
        return d.clone()
    if isinstance(d, dict):
        return {k: copy_state(v) for k, v in d.items()}
    if isinstance(d, list):
        return [copy_state(v) for v in d]
    return d


def evaluate(model: Policy, out: Path, state: dict, seeds: list[str]) -> dict:
    """The most likely action on the evaluation seeds; saved per update number."""
    started = time.perf_counter()
    rows = play(model, [(s, DECK, STAKE) for s in seeds])
    s = summary(rows)
    s["seconds"] = time.perf_counter() - started
    s["update"] = state["updates"]
    s["hours"] = state["seconds"] / 3600
    write_json(out / f"eval_u{state['updates']:05d}.json", {"summary": s, "games": rows})
    shutil.copyfile(out / "latest.pt", out / f"u{state['updates']:05d}.pt")
    print(f"eval u{state['updates']}: {s['games']} seeds, win {s['win rate']:.2%} "
          f"(+/- {s['win rate +/-']:.2%}), mean ante {s['mean final ante']:.3f}, "
          f"{s['seconds']:.0f}s", flush=True)
    return s


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("fit-value", "train"):
        p = sub.add_parser(name)
        p.add_argument("--run", required=True)
        p.add_argument("--slots", type=int, default=64, help="games side by side")
        p.add_argument("--ticks", type=int, default=256, help="steps per game per rollout")
        p.add_argument("--seed", type=int, default=0)
        p.add_argument("--threads", type=int, default=4)
        p.add_argument("--normal-priority", action="store_true")
    fv = sub.choices["fit-value"]
    fv.add_argument("--start", required=True, help="the copy: rl/data/models/<start>.pt")
    fv.add_argument("--games", type=int, default=3000)
    fv.add_argument("--fit-steps", type=int, default=4000)
    tr = sub.choices["train"]
    tr.add_argument("--hours", type=float, required=True)
    tr.add_argument("--start", default="", help="the copy, when there is no fitted start.pt")
    tr.add_argument("--plays", type=int, default=4,
                    help="plays of each seed, scored against each other (1: judge moves by the value)")
    tr.add_argument("--seeds", type=int, default=32, help="seeds per update when --plays > 1")
    tr.add_argument("--games-per-step", type=int, default=0,
                    help="with --plays > 1: games behind each single optimiser step, its size set "
                         "by --target-change (0: PPO passes over --seeds x --plays games)")
    tr.add_argument("--target-change", type=float, default=0.005,
                    help="how far one step may move the policy on its batch (KL estimate)")
    tr.add_argument("--change-sample", type=int, default=4096,
                    help="stored moves the change is measured on")
    tr.add_argument("--resume", action="store_true")
    tr.add_argument("--beta", type=float, default=0.1, help="starting drift penalty")
    tr.add_argument("--fade", type=float, default=0.5,
                    help="share of the run over which beta falls to 0 (0: beta held)")
    tr.add_argument("--passes", type=int, default=4)
    tr.add_argument("--batch", type=int, default=512)
    tr.add_argument("--lr", type=float, default=1e-4)
    tr.add_argument("--clip", type=float, default=0.2)
    tr.add_argument("--lam", type=float, default=0.95)
    tr.add_argument("--value-coef", type=float, default=0.5)
    tr.add_argument("--entropy-coef", type=float, default=0.0)
    tr.add_argument("--max-grad", type=float, default=0.5)
    tr.add_argument("--eval-every", type=float, default=2.0, help="hours between evaluations (0: end only)")
    tr.add_argument("--eval-games", type=int, default=1000)
    args = ap.parse_args()
    torch.set_num_threads(args.threads)
    if not args.normal_priority:
        below_normal_priority()
    if args.cmd == "fit-value":
        return fit_value(args)
    try:
        return train(args)
    except SystemExit as e:
        if e.code not in (0, None):
            report_failure(args.run, str(e.code))
        raise
    except BaseException as e:
        report_failure(args.run, f"{type(e).__name__}: {e}" if str(e) else type(e).__name__)
        raise


if __name__ == "__main__":
    sys.exit(main())
