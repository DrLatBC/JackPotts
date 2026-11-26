"""Big starts: the pool of starting points and the main lines that start from it."""

from __future__ import annotations

import numpy as np
import pytest
import torch
from jackdaw.env.action_space import ActionType
from jackdaw.env.game_spec import FactoredAction

from rl import starts
from rl.branch import BranchPlay
from rl.model import Policy
from rl.playback import make_env
from rl.starts import StartPool


@pytest.fixture(scope="module")
def model() -> Policy:
    torch.manual_seed(0)
    return Policy(width=32, layers=1).eval()


def game(seed: str):
    env = make_env("b_blue", 1, end_on_win=False)
    _, _, info = env.reset(seed=seed)
    return info["game"]


def kept(*seeds: str):
    k = starts.history()
    for s in seeds:
        starts.note_decision(game(s), k)
    return k


def test_only_build_decisions_are_kept_and_only_the_last_few():
    env = make_env("b_blue", 1, end_on_win=False)
    _, _, info = env.reset(seed="RLE-000001")
    k = starts.history()
    starts.note_decision(info["game"], k)
    assert len(k) == 1 and k[0] is not info["game"]
    _, _, _, _, info = env.step(FactoredAction(int(ActionType.SelectBlind)))
    starts.note_decision(info["game"], k)
    assert info["game"].phase.value == "selecting_hand" and len(k) == 1
    for i in range(starts.BACK + 3):
        starts.note_decision(game(f"RLE-{i:06d}"), k)
    assert len(k) == starts.BACK


def test_a_hand_must_reach_the_minimum_ratio_and_its_oldest_copy_goes_in():
    pool = StartPool(3, full=2)
    k = kept("A1", "A2")
    assert not pool.offer(starts.MIN_RATIO - 1, k)
    assert pool.offer(starts.MIN_RATIO, k)
    assert pool.entries[0]["hash"] == k[0].state_hash() != k[1].state_hash()
    assert not pool.offer(50, starts.history())


def test_the_pool_keeps_the_best_and_a_repeat_only_raises_its_ratio():
    pool = StartPool(2, full=2)
    assert pool.offer(30, kept("B1")) and pool.offer(40, kept("B2"))
    assert not pool.offer(25, kept("B3"))
    assert pool.offer(60, kept("B4"))
    assert [e["ratio"] for e in pool.entries] == [60, 40]
    assert not pool.offer(90, kept("B2"))
    assert [e["ratio"] for e in pool.entries] == [90, 60] and len(pool.entries) == 2


def test_draws_wait_for_a_full_pool_and_a_used_entry_decays():
    pool = StartPool(5, full=2)
    pool.offer(30, kept("C1"))
    assert not pool.ready
    pool.offer(40, kept("C2"))
    assert pool.ready
    rng = np.random.default_rng(0)
    drawn = pool.draw(rng)
    hit = next(e for e in pool.entries if e["hash"] == drawn.state_hash())
    assert hit["weight"] == pytest.approx(starts.DECAY) and pool.used == 1
    assert drawn is not hit["game"]


def test_the_pool_saves_and_loads(tmp_path):
    pool = StartPool(5, full=2)
    pool.offer(30, kept("D1"))
    pool.offer(float("inf"), kept("D2"))
    pool.draw(np.random.default_rng(1))
    pool.save(tmp_path / "starts.json.gz")
    back = StartPool(5, full=2)
    back.load(tmp_path / "starts.json.gz")
    assert [(e["ratio"], e["weight"], e["hash"]) for e in back.entries] == \
        [(e["ratio"], e["weight"], e["hash"]) for e in pool.entries]
    assert back.used == 1


def test_a_restart_is_its_own_main_line_and_stays_out_of_fresh_game_stats(model):
    pool = StartPool(5, full=1)
    pool.offer(30, kept("E1"))
    want = pool.entries[0]["hash"]
    bp = BranchPlay("test-starts", seed=1, rate=0.3, mains=2, pool=pool, start_share=1.0)
    main = bp._new_main()
    assert main["start"] and main["seed"].startswith("start-test-starts-")
    assert main["info"]["game"].state_hash() == want
    assert bp.next_seed == 0
    batch = bp.play(model, 4)
    assert batch.finished and all(r["start"] for r in batch.finished)


def test_fresh_lines_feed_the_pool_with_each_hand_over_target(model, monkeypatch):
    pool = StartPool(5, full=5)
    seen = []
    monkeypatch.setattr(pool, "offer", lambda ratio, k: seen.append((ratio, len(k))) or False)
    bp = BranchPlay("test-feed", seed=2, rate=0.0, mains=1, pool=pool, start_share=0.25)
    bp._pairs, bp._states, bp._finished, bp._resolved = [], [], [], []
    main = bp._new_main()
    assert not main["start"]
    bp._step(main, FactoredAction(int(ActionType.SelectBlind)))
    target = main["info"]["state"].blind_chips
    bp._step(main, FactoredAction(int(ActionType.PlayHand), card_target=(0,)))
    scored = main["info"]["game"].last_score.total
    assert seen == [(scored / target, 1)]
