"""Branch play: a copied game replays its original, each untied decision becomes a pair
of examples whose stored log-probabilities learning recomputes, a branch back on its main
line stops tied, and a short branch-trained run writes its files."""

from __future__ import annotations

import json
import sys

import numpy as np
import pytest
import torch

from rl import branch, dataset, ppo
from rl.branch import BranchPlay, tally
from rl.evaluate import DECK, STAKE, game_setup
from rl.model import Policy, Prepared, choose
from rl.playback import copy_env, env_view, make_env
from rl.ppo import scored
from rl.selfplay import game_points
from rl.train import save_model


@pytest.fixture(scope="module")
def model() -> Policy:
    torch.manual_seed(0)
    return Policy(width=32, layers=1).eval()


def play_out(model, env, info):
    while True:
        move = choose(model, Prepared(dataset.examples_of([env_view(info)])))[0]
        _, terminated, truncated, _, info = env.step(move)
        if terminated or truncated:
            return info["game"].state_hash(), env._step_count


def test_a_copied_game_plays_out_exactly_like_the_original(model):
    env = make_env(DECK, STAKE, on_refusal="truncate")
    _, _, info = env.reset(seed="RLE-000002")
    for _ in range(25):
        move = choose(model, Prepared(dataset.examples_of([env_view(info)])))[0]
        _, _, _, _, info = env.step(move)
    copy, copy_info = copy_env(env)
    assert copy_info["game"].state_hash() == info["game"].state_hash()
    assert play_out(model, copy, copy_info) == play_out(model, env, info)


def scored_by_money(monkeypatch):
    """An untrained policy loses every game at ante 1, so every decision would tie: score
    each line by its money at the end instead (as the ante the points count)."""
    real = branch._result

    def result(info, truncated, stuck):
        return {**real(info, truncated, stuck), "won": False, "ante": 1 + int(info["game"].dollars)}

    monkeypatch.setattr(branch, "_result", result)


@pytest.fixture(scope="module")
def batch(model):
    with pytest.MonkeyPatch.context() as mp:
        scored_by_money(mp)
        return BranchPlay("test", seed=3, rate=0.3, mains=4).play(model, 6)


def test_each_untied_decision_is_a_pair_with_opposite_halves_of_its_difference(batch):
    counts = tally(batch.decisions)["all"]
    pairs = counts["main better"] + counts["drawn better"]
    assert pairs >= 6 and len(batch.examples) == 2 * pairs == len(batch.adv) == len(batch.logp)
    assert np.allclose(batch.adv[0::2], -batch.adv[1::2])
    assert (batch.adv != 0).all()
    assert counts["n"] == counts["tied"] + counts["cut"] + pairs


def test_both_moves_of_a_pair_are_legal_and_different(batch):
    ex = batch.examples
    for i in range(0, len(ex), 2):
        a, b = ex.action(i), ex.action(i + 1)
        assert np.array_equal(ex["glob"][i], ex["glob"][i + 1])
        assert ex["type_mask"][i][a.action_type] and ex["type_mask"][i][b.action_type]
        assert (a.action_type, a.entity_target, sorted(a.card_target or ())) != \
            (b.action_type, b.entity_target, sorted(b.card_target or ()))


def test_stored_log_probabilities_are_the_ones_learning_recomputes(model, batch):
    for examples, stored in ((batch.examples, batch.logp), (batch.states, batch.state_logp)):
        prep = Prepared(examples)
        with torch.no_grad():
            logp = scored(model, prep, np.arange(prep.n))[0].numpy()
        assert np.allclose(logp, stored, atol=1e-5)


def test_main_lines_score_with_the_win_bonus(batch):
    assert batch.finished
    for g in batch.finished:
        assert g["points"] == game_points(g, 8.0)


def test_a_branch_back_on_its_main_lines_path_stops_tied():
    bp = BranchPlay("test")
    bp.seeds["s"] = {"result": None, "hash": {(2, 4): "h"}, "open": 1, "waiting": []}
    bp._resolved, bp._pairs = [], []
    decision = {"phase": "shop", "ante": 2, "round": 4, "main move": "NextRound",
                "drawn move": "Reroll", "pair": (None, None, 0.0, None, 0.0)}
    line = {"role": "branch", "seed": "s", "decision": decision, "hashes": {(2, 4): "other"}}
    assert not bp._joined(line)
    line["hashes"][(3, 7)] = "x"
    bp.seeds["s"]["hash"][(3, 7)] = "x"
    assert bp._joined(line)
    assert bp._resolved == [decision] and decision["tied"] and decision["joined"]
    assert bp._pairs == [] and bp.seeds["s"]["open"] == 0


def test_the_panels_deck_and_stake_names_map_to_the_engines():
    assert game_setup("blue", "WHITE") == ("b_blue", 1)
    assert game_setup("PLASMA", "gold") == ("b_plasma", 8)


def test_a_short_branch_run_writes_its_log_decisions_and_status(model, tmp_path, monkeypatch):
    monkeypatch.setattr(ppo, "TRAINING", tmp_path / "training")
    monkeypatch.setattr(ppo, "PPO", tmp_path / "ppo")
    scored_by_money(monkeypatch)
    start = tmp_path / "start.pt"
    save_model(start, model, {"width": 32, "layers": 1}, {"training games": 0})
    monkeypatch.setattr(sys, "argv", [
        "rl.ppo", "train", "--run", "br", "--start", str(start), "--hours", "0.00001",
        "--branch-rate", "0.3", "--decisions-per-step", "4", "--win-bonus", "16",
        "--deck", "red", "--stake", "red", "--eval-every", "0", "--eval-games", "2",
        "--normal-priority", "--threads", "2",
    ])
    assert ppo.main() == 0
    out = tmp_path / "ppo" / "br"
    line = json.loads((out / "log.jsonl").read_text().splitlines()[-1])
    assert line["untied"] >= 4 and 0 <= line["tied share"] <= 1 and "change" in line
    decisions = json.loads((out / "decisions.jsonl").read_text().splitlines()[-1])
    assert decisions["update"] == 1 and decisions["all"]["n"] == line["decisions"]
    state = json.loads((out / "state.json").read_text())
    assert state["args"]["deck"] == "RED" and state["args"]["win_bonus"] == 16.0
    assert json.loads((tmp_path / "training" / "br.json").read_text())["phase"] == "finished"
