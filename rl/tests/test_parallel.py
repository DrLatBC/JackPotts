"""Parallel branch play: seeds split across workers, batches joined, start pools merged."""

from __future__ import annotations

import numpy as np
import pytest

from rl import starts
from rl.branch import BranchPlay
from rl.parallel import join_batches, merge_pools
from rl.selfplay import train_seed
from rl.tests.test_branch import batch, model, scored_by_money  # noqa: F401


def test_a_worker_plays_every_stride_th_seed_from_its_first():
    bp = BranchPlay("test", first=1, stride=3)
    seeds = [bp._new_main()["seed"] for _ in range(3)]
    assert seeds == [train_seed("test", n) for n in (1, 4, 7)]
    assert bp.next_seed == 10


def test_joined_batches_keep_every_example_and_renumber_games(batch):  # noqa: F811
    both = join_batches([batch, batch])
    assert len(both.examples) == 2 * len(batch.examples) == len(both.adv) == len(both.logp)
    assert len(both.states) == 2 * len(batch.states) == len(both.state_logp)
    assert np.array_equal(both.adv, np.concatenate([batch.adv, batch.adv]))
    games = batch.examples.n_games
    assert both.examples.n_games == 2 * games
    assert both.examples["game"][len(batch.examples):].min() >= games
    assert len(both.decisions) == 2 * len(batch.decisions)


def entry(h: str, ratio: float, weight: float = 1.0) -> dict:
    return {"hash": h, "ratio": ratio, "weight": weight, "game": None}


def copy_of(pool: starts.StartPool) -> starts.StartPool:
    c = starts.StartPool(pool.size)
    c.entries, c.used = [dict(e) for e in pool.entries], pool.used
    return c


def test_merged_pools_keep_every_new_entry_and_every_draws_decay():
    pool = starts.StartPool(3)
    pool.entries, pool.used = [entry("a", 9.0), entry("b", 6.0)], 5
    one, two = copy_of(pool), copy_of(pool)
    one.entries[0]["weight"] *= 0.6
    one.used += 1
    two.entries[0]["weight"] *= 0.6
    two.entries[1]["weight"] *= 0.6
    two.used += 2
    one.entries.append(entry("c", 7.0))
    two.entries.append(entry("d", 8.0))
    merge_pools(pool, [one, two])
    got = {e["hash"]: e for e in pool.entries}
    assert [e["hash"] for e in pool.entries] == ["a", "d", "c"]
    assert got["a"]["weight"] == pytest.approx(0.36)
    assert pool.used == 8


def test_an_entry_both_workers_found_keeps_its_best_ratio():
    pool = starts.StartPool(5)
    one, two = copy_of(pool), copy_of(pool)
    one.entries.append(entry("x", 6.0))
    two.entries.append(entry("x", 7.5))
    merge_pools(pool, [one, two])
    assert [(e["hash"], e["ratio"]) for e in pool.entries] == [("x", 7.5)]
