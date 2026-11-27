"""Branch play split over worker processes, joined into one learning step.

One process plays about as fast with 2 torch threads as with 4, so a step's decisions come
in about ``workers`` times as fast when each worker plays its share side by side. Each
worker keeps its own ``BranchPlay`` (its lines carry over between updates, as they do in
one process) and gets the current weights before each share.

Seeds: worker ``w`` plays training seeds ``first + w``, ``first + w + workers``, ... (and
big-start names the same way), so no two workers play one seed. The run saves the highest
next seed any worker reached; a resume starts every worker past it.

Big starts: the run keeps the one pool (``rl.starts``). Each share starts from a copy of it
in every worker; afterwards the copies' new entries are merged in, the best ``size`` by
ratio kept, and an entry's weight decays by every draw any worker made of it.
"""

from __future__ import annotations

import multiprocessing as mp
import traceback

import numpy as np
import torch

from jackdaw.core.game import Game

from rl import dataset, starts
from rl.branch import Batch, BranchPlay
from rl.model import Policy


def join(parts: list[dataset.Examples]) -> dataset.Examples:
    """Examples from several writers as one set, games renumbered in order."""
    return dataset.join([p.arrays for p in parts])


def join_batches(batches: list[Batch]) -> Batch:
    return Batch(join([b.examples for b in batches]),
                 np.concatenate([b.logp for b in batches]),
                 np.concatenate([b.adv for b in batches]),
                 join([b.states for b in batches]),
                 np.concatenate([b.state_logp for b in batches]),
                 [g for b in batches for g in b.finished],
                 [d for b in batches for d in b.decisions])


# -- the start pool across workers --------------------------------------------------


def pool_rows(pool: starts.StartPool) -> dict:
    return {"used": pool.used, "entries": [{"ratio": e["ratio"], "weight": e["weight"],
                                            "game": e["game"].to_dict()} for e in pool.entries]}


def set_pool(pool: starts.StartPool, rows: dict) -> None:
    pool.used = rows["used"]
    pool.entries = []
    for row in rows["entries"]:
        game = Game.from_dict(row["game"])
        pool.entries.append({"ratio": row["ratio"], "weight": row["weight"],
                             "hash": game.state_hash(), "game": game})


def merge_pools(pool: starts.StartPool, copies: list[starts.StartPool]) -> None:
    """Fold the workers' copies of ``pool`` back into it: every entry any copy holds, each
    at its best ratio, its weight the pool's times each copy's decay of it, then the best
    ``size`` by ratio."""
    sent = {e["hash"]: e for e in pool.entries}
    merged: dict = {}
    for copy in copies:
        for e in copy.entries:
            base = sent[e["hash"]]["weight"] if e["hash"] in sent else 1.0
            have = merged.get(e["hash"])
            if have is None:
                merged[e["hash"]] = dict(e)
            else:
                have["ratio"] = max(have["ratio"], e["ratio"])
                have["weight"] *= e["weight"] / base
    for h, e in sent.items():
        merged.setdefault(h, e)
    pool.entries = sorted(merged.values(), key=lambda e: -e["ratio"])[:pool.size]
    pool.used += sum(c.used - pool.used for c in copies)


# -- workers ------------------------------------------------------------------------


def _worker(conn, kw: dict, threads: int) -> None:
    torch.set_num_threads(threads)
    torch.manual_seed(kw["seed"])
    pool_size = kw.pop("pool_size")
    pool = starts.StartPool(pool_size) if pool_size else None
    games = BranchPlay(**kw, pool=pool)
    model = None
    while True:
        msg = conn.recv()
        if msg is None:
            return
        try:
            config, state, decisions, rows = msg
            if model is None:
                model = Policy(**config)
            model.load_state_dict(state)
            model.eval()
            if pool is not None:
                set_pool(pool, rows)
            batch = games.play(model, decisions)
            conn.send(("ok", batch, games.next_seed, games.next_start,
                       pool_rows(pool) if pool is not None else None))
        except Exception:
            conn.send(("error", traceback.format_exc()))


class ParallelPlay:
    """``BranchPlay``'s ``play`` over ``workers`` processes of ``threads`` torch threads.
    Takes ``BranchPlay``'s arguments; ``next_seed`` and ``next_start`` may be set before the
    first ``play`` (a resume), and read after each as the run's next unused ones."""

    def __init__(self, run: str, *, workers: int, threads: int = 2, seed: int = 0,
                 first: int = 0, pool: starts.StartPool | None = None, **kw) -> None:
        self.run, self.workers, self.threads, self.seed = run, workers, threads, seed
        self.next_seed, self.next_start = first, 0
        self.pool, self.kw = pool, kw
        self.procs: list = []
        self.conns: list = []

    def _start(self) -> None:
        ctx = mp.get_context("spawn")
        for w in range(self.workers):
            parent, child = ctx.Pipe()
            kw = {**self.kw, "seed": self.seed * 1000 + w, "first": self.next_seed + w,
                  "stride": self.workers, "first_start": self.next_start + w,
                  "pool_size": self.pool.size if self.pool is not None else 0}
            p = ctx.Process(target=_worker, args=(child, {"run": self.run, **kw}, self.threads),
                            daemon=True)
            p.start()
            self.procs.append(p)
            self.conns.append(parent)

    def play(self, model: Policy, decisions: int) -> Batch:
        """Play until about ``decisions`` untied decisions have resolved, split evenly."""
        if not self.procs:
            self._start()
        state = {k: v.detach().clone() for k, v in model.state_dict().items()}
        rows = pool_rows(self.pool) if self.pool is not None else None
        share = -(-decisions // self.workers)
        for conn in self.conns:
            conn.send((model.config, state, share, rows))
        got = [conn.recv() for conn in self.conns]
        failed = [g[1] for g in got if g[0] == "error"]
        if failed:
            self.close()
            raise RuntimeError("a play worker failed:\n" + failed[0])
        self.next_seed = max(g[2] for g in got)
        self.next_start = max(g[3] for g in got)
        if self.pool is not None:
            copies = []
            for g in got:
                c = starts.StartPool(self.pool.size)
                set_pool(c, g[4])
                copies.append(c)
            merge_pools(self.pool, copies)
        return join_batches([g[1] for g in got])

    def close(self) -> None:
        for conn in self.conns:
            try:
                conn.send(None)
            except (BrokenPipeError, OSError):
                pass
        for p in self.procs:
            p.join(timeout=10)
            if p.is_alive():
                p.terminate()
        self.procs, self.conns = [], []
