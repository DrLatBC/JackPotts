"""Self-play: sampled actions are legal, their recorded log-probabilities are the ones
learning recomputes, points add up, and a PPO update starts from those numbers."""

from __future__ import annotations

import dataclasses
from argparse import Namespace

import numpy as np
import pytest
import torch

from jackdaw.env.action_space import ActionType

from rl.evaluate import eval_seed
from rl.model import VALUE_SCALE, Policy, Prepared, act, choose
from rl.ppo import one_step, scored, seed_share, update
from rl.selfplay import (WIN_BONUS, Games, Rollout, SelfPlay, advantages, points,
                         same_seed_scores, train_seed)


@pytest.fixture(scope="module")
def model() -> Policy:
    torch.manual_seed(0)
    return Policy(width=32, layers=1).eval()


@pytest.fixture(scope="module")
def rollout(model) -> Rollout:
    return SelfPlay("test", seed=1).collect(model, games=3, ticks=60)


def test_sampled_actions_are_legal(rollout):
    ex = rollout.examples
    assert len(ex) == 180
    for i in range(len(ex)):
        action = ex.action(i)
        t = action.action_type
        assert ex["type_mask"][i][t]
        if action.entity_target is not None:
            assert ex.entity_mask(i, t)[action.entity_target]
        if t in (ActionType.PlayHand, ActionType.Discard):
            cards = action.card_target
            assert 1 <= len(cards) <= 5
            assert ex.rows("card_mask", i)[list(cards)].all()
    assert not any(g["cut"] for g in rollout.finished)


def test_recorded_log_probabilities_are_the_ones_learning_recomputes(model, rollout):
    prep = Prepared(rollout.examples)
    with torch.no_grad():
        logp, ent, value = scored(model, prep, np.arange(prep.n))
    assert np.allclose(logp.numpy(), rollout.logp, atol=1e-5)
    assert np.allclose(value.numpy() * VALUE_SCALE, rollout.value, atol=1e-4)
    assert (ent.numpy() >= -1e-6).all()
    assert (rollout.logp <= 1e-6).all()


def test_most_likely_play_is_unchanged_by_sampling_support(model, rollout):
    prep = Prepared(rollout.examples)
    greedy, logp, _ = act(model, prep)
    assert greedy == choose(model, prep)
    assert np.isfinite(logp).all()


def test_a_game_is_worth_its_antes_cleared_plus_the_win_bonus(model):
    games = SelfPlay("test-whole", seed=2).whole_games(model, total=3, parallel=2)
    assert len(games.results) == 3 and all(r is not None for r in games.results)
    to_go = games.points_to_go()
    for g, result in enumerate(games.results):
        steps = np.flatnonzero(games.game == g)
        assert len(steps) == result["steps"]
        expected = result["ante"] - 1 + (WIN_BONUS if result["won"] else 0.0)
        assert result["points"] == expected == games.reward[steps].sum() == to_go[steps[0]]
    assert len(games.examples) == len(games.reward)


def _step(won_before, *, beaten=False, boss=False, won_now=None, move=ActionType.PlayHand):
    from types import SimpleNamespace
    return SimpleNamespace(prev=SimpleNamespace(won=won_before), blind_beaten=beaten,
                           boss_beaten=boss, run_won=bool(won_now) and not won_before,
                           action=SimpleNamespace(action_type=move))


def test_endless_blinds_earn_a_third_of_the_endless_bonus_each():
    assert points(_step(False, beaten=True, boss=True), 8.0, 4.0) == 1.0
    assert points(_step(False, beaten=True, boss=True, won_now=True), 8.0, 4.0) == 9.0
    assert points(_step(False, beaten=True), 8.0, 4.0) == 0.0
    assert points(_step(True, beaten=True), 8.0, 4.0) == points(_step(True, beaten=True, boss=True), 8.0, 4.0)         == pytest.approx(4.0 / 3)
    assert points(_step(True, move=ActionType.SkipBlind), 8.0, 4.0) == pytest.approx(4.0 / 3)
    assert points(_step(True), 8.0, 4.0) == 0.0
    assert points(_step(True, beaten=True), 8.0, 0.0) == 0.0


def test_an_endless_bonus_plays_won_games_on():
    assert SelfPlay("test")._new_game()["env"]._end_on_win
    assert not SelfPlay("test", endless_bonus=4.0)._new_game()["env"]._end_on_win


def test_repeated_seeds_are_played_in_groups(model):
    games = SelfPlay("test-repeat", seed=3).whole_games(model, total=4, parallel=4, repeat=2)
    seeds = [r["seed"] for r in games.results]
    assert seeds[0] == seeds[1] != seeds[2] == seeds[3]
    assert len(games.logp) == len(games.reward)


def test_a_game_is_scored_against_the_other_plays_of_its_seed():
    # Two seeds, two plays each: points 3 and 1 on the first seed, 5 and 5 on the second.
    games = Games(examples=None, reward=np.array([1, 2, 1, 2, 3, 5], np.float32),
                  game=np.array([0, 0, 1, 2, 2, 3]), results=[{}] * 4,
                  logp=np.zeros(6, np.float32))
    score, total = same_seed_scores(games, 2)
    assert total.tolist() == [3, 1, 5, 5]
    assert score.tolist() == [2, 2, -2, 0, 0, 0]
    penalised, _ = same_seed_scores(games, 2, penalty=np.array([0, 0, 0, 1, 0, 0], np.float32))
    assert penalised.tolist() == [2, 2, -2, -1, -1, 1]
    with pytest.raises(ValueError):
        same_seed_scores(games, 3)


def test_seed_share_tells_luck_from_choices():
    assert seed_share(np.array([[1.0, 1.0], [5.0, 5.0], [3.0, 3.0]])) == pytest.approx(1.0)
    assert seed_share(np.array([[1.0, 5.0], [5.0, 1.0], [1.0, 5.0], [5.0, 1.0]])) == 0.0


def test_advantages_follow_each_game_and_stop_where_it_ends():
    # One slot, four ticks: a point at tick 1, the game ends at tick 2, a new game after.
    r = Rollout(examples=None, logp=np.zeros(4, np.float32), value=np.full(4, 0.5, np.float32),
                reward=np.array([0, 1, 0, 0], np.float32), done=np.array([0, 0, 1, 0], bool),
                last_value=np.array([2.0], np.float32), games=1)
    adv, ret = advantages(r, lam=1.0)
    assert np.allclose(ret, [1.0, 1.0, 0.0, 2.0])
    assert np.allclose(adv, ret - 0.5)


def test_seeds_never_look_like_the_games_own():
    for s in (train_seed("r1", 0), eval_seed(1)):
        assert "-" in s and len(s) != 8
    assert train_seed("r1", 5) != train_seed("r2", 5)
    assert eval_seed(1) == "RLE-000001"


def test_an_update_starts_from_the_recorded_probabilities(model, rollout):
    """With no learning rate the policy cannot move, so every ratio is exactly 1."""
    prep = Prepared(rollout.examples)
    adv, ret = advantages(rollout)
    trial = Policy(width=32, layers=1)
    trial.load_state_dict(model.state_dict())
    opt = torch.optim.Adam(trial.parameters(), lr=0.0)
    args = Namespace(passes=1, batch=64, clip=0.2, value_coef=0.5, entropy_coef=0.0, max_grad=0.5)
    stats = update(trial, opt, prep, rollout.logp, adv, ret, args, np.random.default_rng(0))
    assert stats["ratio"] == pytest.approx(1.0, abs=1e-5)
    assert stats["clipped"] == 0.0


def _stuck_after(steps, make_env):
    """``make_env`` whose game offers no legal action after ``steps`` steps."""
    def stuck_env(*a, **k):
        env = make_env(*a, **k)
        step, taken = env.step, [0]

        def stepped(action):
            out = list(step(action))
            taken[0] += 1
            if taken[0] == steps and not (out[1] or out[2]):
                info = dict(out[4])
                mask = info["action_mask"]
                info["action_mask"] = dataclasses.replace(
                    mask, type_mask=np.zeros_like(mask.type_mask))
                out[4] = info
            return tuple(out)
        env.step = stepped
        return env
    return stuck_env


def test_a_game_with_no_legal_action_ends_cut_short(model, monkeypatch):
    import rl.evaluate as ev
    import rl.selfplay as sp
    monkeypatch.setattr(sp, "make_env", _stuck_after(3, sp.make_env))
    games = SelfPlay("test-stuck", seed=4).whole_games(model, total=2, parallel=2)
    assert [(r["steps"], r["cut"]) for r in games.results] == [(3, "no legal action")] * 2
    monkeypatch.setattr(ev, "make_env", _stuck_after(3, ev.make_env))
    rows = ev.play(model, [(eval_seed(1), ev.DECK, ev.STAKE)])
    assert [(r["steps"], r["cut"], r["won"]) for r in rows] == [(3, "no legal action", False)]


def _one_step(model, rollout, lr):
    prep = Prepared(rollout.examples)
    trial = Policy(width=32, layers=1)
    trial.load_state_dict(model.state_dict())
    trial.eval()
    opt = torch.optim.Adam(trial.parameters(), lr=lr)
    adv = np.random.default_rng(5).normal(size=prep.n).astype(np.float32)
    args = Namespace(batch=64, max_grad=0.5, change_sample=100, target_change=0.005, lr=lr)
    state = {}
    stats = one_step(trial, opt, prep, rollout.logp, adv, state, args, np.random.default_rng(0))
    sample = np.sort(np.random.default_rng(0).choice(prep.n, 100, replace=False))
    with torch.no_grad():
        log_r = scored(trial, prep, sample)[0] - torch.from_numpy(rollout.logp[sample])
    return stats, state, float(((log_r.exp() - 1) - log_r).mean())


def test_a_step_that_moves_too_far_is_undone_and_retried_smaller(model, rollout):
    stats, state, change = _one_step(model, rollout, lr=1.0)
    assert stats["tries"] > 1 and stats["lr"] < 1.0
    assert stats["change"] == pytest.approx(change, rel=1e-4, abs=1e-7)
    assert stats["change"] <= 0.01 or stats["tries"] == 5


def test_a_step_that_barely_moves_doubles_the_next_one(model, rollout):
    stats, state, change = _one_step(model, rollout, lr=1e-9)
    assert stats["tries"] == 1 and change < 0.005
    assert state["step lr"] == pytest.approx(2e-9)
