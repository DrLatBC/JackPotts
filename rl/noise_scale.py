"""How many branch decisions one step needs before its direction is evidence, not noise.

    .venv-rl\\Scripts\\python.exe -m rl.noise_scale --save rl/data/ppo/night7/u00006.pt --run night7

Plays ``--chunks`` batches of ``--chunk`` untied decisions from one save, the way a branch
run collects them (the run's own reward, branch rate and big-starts pool, read from its
``state.json``), and takes the policy gradient of each batch as one training step would,
before clipping and Adam. Workers play on their own training seeds, side by side.

From those gradients (McCandlish et al. 2018, "An Empirical Model of Large-Batch
Training"): the true gradient's squared length is the mean dot product of two different
batches' gradients, the noise is how much longer a single batch's gradient is than that,
and their ratio is the noise scale, in decisions. A step of B decisions points about
B / (B + noise scale) of the way along the true direction (split-half agreement: the
cosine between two independent B-decision gradients), so steps well under the noise
scale are mostly noise and steps well over it buy little more. The report gives the
noise scale with a bootstrap range over chunks, and the measured split-half cosine at
each size the chunks allow.

Writes ``rl/data/noise/<label>.npz`` (per-chunk gradient sums, example and decision
counts) and ``<label>.json`` (the report). ``--report`` recomputes the report from a saved
``.npz`` without playing.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import sys
import time
from pathlib import Path

import numpy as np
import torch

DATA = Path(__file__).resolve().parent / "data"
OUT = DATA / "noise"
PPO = DATA / "ppo"


def flat_grad(model) -> np.ndarray:
    return torch.cat([p.grad.reshape(-1) if p.grad is not None else torch.zeros(p.numel())
                      for p in model.parameters()]).numpy().astype(np.float64)


def gradient_sum(model, prep, adv: np.ndarray, batch: int = 512) -> np.ndarray:
    """The sum over examples of adv times the gradient of the move's log-probability:
    ``one_step``'s policy-gradient loss before its division by the example count."""
    from rl.ppo import scored
    model.train()
    model.zero_grad(set_to_none=True)
    adv_t = torch.from_numpy(adv.astype(np.float32))
    for at in range(0, prep.n, batch):
        idx = np.arange(at, min(at + batch, prep.n))
        logp, _, _ = scored(model, prep, idx)
        (-(adv_t[torch.from_numpy(idx)] * logp).sum()).backward()
    model.eval()
    return flat_grad(model)


def _worker(job: dict) -> list[dict]:
    from rl.branch import BranchPlay, tally
    from rl.evaluate import game_setup, model_file
    from rl.starts import StartPool
    from rl.train import load_model

    torch.set_num_threads(job["threads"])
    torch.manual_seed(job["worker"])
    model, _ = load_model(model_file(job["save"]))
    model.eval()
    a = job["args"]
    deck, stake = game_setup(a["deck"], a["stake"])
    pool = None
    if a.get("start_share"):
        pool = StartPool(a.get("start_pool", 200))
        pool.load(Path(job["pool"]))
    games = BranchPlay(f"noise-{job['label']}", seed=1000 + job["worker"],
                       first=job["worker"] * 1_000_000, deck=deck, stake=stake,
                       win_bonus=a["win_bonus"], endless_bonus=a.get("endless_bonus", 0.0),
                       overkill=a.get("overkill", 0.0), rate=a["branch_rate"],
                       draws=a.get("draws", 4), pool=pool, start_share=a.get("start_share", 0.0))
    out = []
    for c in range(job["chunks"]):
        t0 = time.perf_counter()
        b = games.play(model, job["chunk"])
        whole = tally(b.decisions)["all"]
        out.append({"adv": b.adv, "examples": b.examples,
                    "decisions": whole["main better"] + whole["drawn better"],
                    "seconds": time.perf_counter() - t0})
        print(f"worker {job['worker']} chunk {c + 1}/{job['chunks']}: {out[-1]['decisions']} decisions "
              f"in {out[-1]['seconds']:.0f}s", flush=True)
    return out


def noise_scale(sums: np.ndarray, examples: np.ndarray, decisions: np.ndarray) -> dict:
    """The true gradient's squared length, the noise and their ratio (decisions), from
    per-chunk gradient sums (rows) over ``examples`` examples holding ``decisions``
    decisions each. A chunk's gradient is its sum over its examples, as one step takes it."""
    g = sums / examples[:, None]
    k = len(g)
    gram = g @ g.T
    off = gram[~np.eye(k, dtype=bool)]
    true_sq = float(off.mean())
    per_chunk = np.diag(gram)
    noise = float(np.mean(decisions * (per_chunk - true_sq)))
    return {"true sq": true_sq, "noise": noise,
            "noise scale": noise / true_sq if true_sq > 0 else float("inf")}


def split_cosines(sums: np.ndarray, examples: np.ndarray, decisions: np.ndarray,
                  rng: np.random.Generator, tries: int = 200) -> list[dict]:
    """Per half size the chunks allow (1, 2, 4, ... chunks a side): the mean cosine between
    the gradients of two disjoint random sets of chunks, and the decisions per side."""
    k = len(sums)
    rows = []
    size = 1
    while 2 * size <= k:
        cos, dec = [], []
        for _ in range(tries):
            pick = rng.permutation(k)
            a, b = pick[:size], pick[size:2 * size]
            ga = sums[a].sum(0) / examples[a].sum()
            gb = sums[b].sum(0) / examples[b].sum()
            cos.append(float(ga @ gb / (np.linalg.norm(ga) * np.linalg.norm(gb))))
            dec.append(float(decisions[a].sum() + decisions[b].sum()) / 2)
        rows.append({"decisions per side": float(np.mean(dec)), "cosine": float(np.mean(cos)),
                     "cosine sd": float(np.std(cos))})
        size *= 2
    return rows


def report(sums: np.ndarray, examples: np.ndarray, decisions: np.ndarray, seed: int = 0,
           boot: int = 500) -> dict:
    rng = np.random.default_rng(seed)
    whole = noise_scale(sums, examples, decisions)
    scales = []
    k = len(sums)
    for _ in range(boot):
        pick = rng.choice(k, k, replace=True)
        if len(set(pick)) < 2:
            continue
        # a chunk drawn twice would read its own dot product as two chunks agreeing
        uniq = np.unique(pick)
        scales.append(noise_scale(sums[uniq], examples[uniq], decisions[uniq])["noise scale"])
    scales = np.array(scales)
    finite = scales[np.isfinite(scales) & (scales > 0)]
    b = whole["noise scale"]
    return {
        **whole,
        "noise scale low": float(np.percentile(finite, 10)) if len(finite) else None,
        "noise scale high": float(np.percentile(finite, 90)) if len(finite) else None,
        "bootstrap negative share": float(np.mean(~(np.isfinite(scales) & (scales > 0)))),
        "chunks": int(k), "decisions": int(decisions.sum()),
        "split halves": split_cosines(sums, examples, decisions, rng),
        "predicted cosine": {str(n): (n / (n + b) if b > 0 and np.isfinite(b) else None)
                             for n in (2000, 6000, 12000, 24000, 48000, 96000)},
    }


def measure(args) -> int:
    from rl.evaluate import model_file
    from rl.model import Prepared
    from rl.train import load_model

    run_dir = PPO / args.run
    run_args = json.loads((run_dir / "state.json").read_text())["args"]
    label = args.label or f"{Path(args.save).parent.name}-{Path(args.save).stem}"
    per = -(-args.chunks // args.workers)
    jobs = [{"worker": w, "save": args.save, "args": run_args, "label": label,
             "pool": str(run_dir / "starts.json.gz"), "threads": args.threads,
             "chunks": min(per, args.chunks - w * per), "chunk": args.chunk}
            for w in range(args.workers) if args.chunks - w * per > 0]
    t0 = time.perf_counter()
    with mp.get_context("spawn").Pool(len(jobs)) as pool:
        played = [c for chunks in pool.map(_worker, jobs) for c in chunks]
    play_s = time.perf_counter() - t0
    model, _ = load_model(model_file(args.save))
    torch.set_num_threads(args.threads * args.workers)
    every = np.concatenate([c["adv"] for c in played])
    mean, std = every.mean(), every.std() + 1e-8
    preps = [Prepared(c["examples"]) for c in played]
    sums = np.stack([gradient_sum(model, p, (c["adv"] - mean) / std) for p, c in zip(preps, played)])
    examples = np.array([p.n for p in preps], np.float64)
    decisions = np.array([c["decisions"] for c in played], np.float64)
    OUT.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(OUT / f"{label}.npz", sums=sums, examples=examples, decisions=decisions)
    rep = {"save": args.save, "run": args.run, "play s": play_s, **report(sums, examples, decisions)}
    (OUT / f"{label}.json").write_text(json.dumps(rep, indent=1))
    print(describe(rep), flush=True)
    return 0


def describe(rep: dict) -> str:
    lines = [f"{rep.get('save', '')}: noise scale {rep['noise scale']:,.0f} decisions "
             f"(10-90%: {rep['noise scale low'] or float('nan'):,.0f} to "
             f"{rep['noise scale high'] or float('nan'):,.0f}) from {rep['decisions']:,} decisions "
             f"in {rep['chunks']} chunks"]
    for r in rep["split halves"]:
        lines.append(f"  {r['decisions per side']:>8,.0f} a side: halves agree cos {r['cosine']:.3f} "
                     f"(sd {r['cosine sd']:.3f})")
    lines.append("  predicted cos at " + ", ".join(
        f"{int(n):,}: {c:.2f}" for n, c in rep["predicted cosine"].items() if c is not None))
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--save", help="the model: a run's u#####.pt or a copy's name")
    ap.add_argument("--run", help="the branch run whose settings and pool the play uses")
    ap.add_argument("--label", default="", help="output name (default <run folder>-<save>)")
    ap.add_argument("--chunks", type=int, default=16)
    ap.add_argument("--chunk", type=int, default=3000, help="untied decisions per chunk")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--threads", type=int, default=2, help="torch threads per worker")
    ap.add_argument("--report", help="recompute the report from this .npz instead of playing")
    args = ap.parse_args()
    if args.report:
        z = np.load(args.report)
        print(describe(report(z["sums"], z["examples"], z["decisions"])))
        return 0
    if not (args.save and args.run):
        ap.error("--save and --run are needed to play")
    return measure(args)


if __name__ == "__main__":
    sys.exit(main())
