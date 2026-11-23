"""Branch play: a copied game replays its original, each untied decision becomes a pair
of examples whose stored log-probabilities learning recomputes, a branch back on its main
line stops tied, and a short branch-trained run writes its files."""

from __future__ import annotations

import json
import sys

import numpy as np
import pytest
import torch
from jackdaw.env.action_space import ActionType

from rl import branch, dataset, ppo
from rl.branch import BranchPlay, tally
from rl.evaluate import DECK, STAKE, game_setup
from rl.model import Policy, Prepared, act, choose
from rl.playback import copy_env, env_view, make_env
from rl.ppo import scored
from rl.train import save_model
from balatro_bot import jobs


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
    each step by the money it made instead."""
    monkeypatch.setattr(branch, "points", lambda t, *_: float(t.curr.dollars - t.prev.dollars))


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


def test_main_lines_score_their_antes_and_the_win_bonus(model):
    bp = BranchPlay("test-points", seed=1, rate=0.0, mains=2)
    bp._pairs, bp._states, bp._finished, bp._resolved = [], [], [], []
    while len(bp._finished) < 2:
        bp._tick(model)
    for g in bp._finished:
        assert g["points"] == g["ante"] - 1 + (8.0 if g["won"] else 0.0)


def test_an_endless_bonus_plays_won_games_on():
    assert BranchPlay("test")._new_main()["env"]._end_on_win
    main = BranchPlay("test", endless_bonus=4.0)._new_main()
    assert not main["env"]._end_on_win and not copy_env(main["env"])[0]._end_on_win


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


def test_a_legal_boss_reroll_is_branched_on_once_per_ante_and_learned_like_any_move(model, monkeypatch):
    import copy

    model = copy.deepcopy(model)
    with torch.no_grad():
        model.type_head.bias[branch.REROLL] -= 30.0
    monkeypatch.setitem(branch.FORCED, branch.REROLL, ("ante", 1.0))
    bp = BranchPlay("test", rate=0.0, mains=1)
    bp._pairs, bp._states, bp._finished, bp._resolved = [], [], [], []
    main = bp._new_main()
    game = main["info"]["game"]
    snapshot = game.to_dict()
    snapshot["sets"]["used_vouchers"] = ["v_directors_cut"]
    game.replace_state(snapshot)
    game.debug_set(money=30)
    main["env"], main["info"] = copy_env(main["env"])
    bp.lines = [main]
    view = env_view(main["info"])
    assert main["info"]["action_mask"].type_mask[branch.REROLL]

    bp._tick(model)
    branches = [line for line in bp.lines if line["role"] == "branch"]
    assert len(branches) == 1 and branches[0]["force"].action_type == branch.REROLL
    assert branches[0]["decision"]["drawn move"] == "RerollBoss" and bp._states == []
    _, move, move_logp, alt, alt_logp = branches[0]["decision"]["pair"]
    prep = Prepared(dataset.examples_of([view]))
    assert bp._forced(model, prep, [move]) == {}

    ex, logp, _ = branch._pair_examples([(view, move, move_logp, alt, alt_logp, 1.0)], DECK, STAKE)
    with torch.no_grad():
        recomputed = scored(model, Prepared(ex), np.arange(2))[0].numpy()
    assert ex.action(1).action_type == branch.REROLL
    assert np.allclose(recomputed, logp, atol=1e-5)


def at_first_shop(bp: BranchPlay) -> dict:
    """The first main line of run "test", its first blind won outright, in its first shop
    (which sells a Planet)."""
    from jackdaw.env.game_spec import FactoredAction

    main = bp._new_main()
    for move in (ActionType.SelectBlind, ActionType.PlayHand, ActionType.CashOut):
        if move == ActionType.PlayHand:
            main["info"]["game"].debug_set(chips=1e9)
            main["env"], main["info"] = copy_env(main["env"])
        cards = (0,) if move == ActionType.PlayHand else None
        bp._step(main, FactoredAction(int(move), card_target=cards))
    return main


@pytest.mark.parametrize("forced, phase", [(branch.SKIP, "blind_select"),
                                           (branch.BUY_AND_USE, "shop")])
def test_a_forced_move_comes_up_once_per_occasion_with_its_chance(model, monkeypatch, forced, phase):
    import copy

    model = copy.deepcopy(model)
    with torch.no_grad():
        model.type_head.bias[forced] -= 30.0
    bp = BranchPlay("test", rate=0.0, mains=1)
    bp._pairs, bp._states, bp._finished, bp._resolved = [], [], [], []
    main = bp._new_main() if phase == "blind_select" else at_first_shop(bp)
    assert main["info"]["game"].phase.value == phase
    assert main["info"]["action_mask"].type_mask[forced]
    bp.lines = [main]
    prep = Prepared(dataset.examples_of([env_view(main["info"])]))
    greedy = act(model, prep)[0]

    monkeypatch.setitem(branch.FORCED, forced, (branch.FORCED[forced][0], 0.0))
    assert bp._forced(model, prep, greedy) == {}
    main["forced"].clear()
    monkeypatch.setitem(branch.FORCED, forced, (branch.FORCED[forced][0], 1.0))
    got = bp._forced(model, prep, greedy)
    assert [move.action_type for move, _ in got.values()] == [forced]
    assert bp._forced(model, prep, greedy) == {}

    (move, logp), = got.values()
    view = env_view(main["info"])
    ex, stored, _ = branch._pair_examples([(view, greedy[0], 0.0, move, logp, 1.0)], DECK, STAKE)
    with torch.no_grad():
        assert np.isclose(scored(model, Prepared(ex), np.arange(2))[0].numpy()[1], stored[1], atol=1e-5)


def test_the_panels_deck_and_stake_names_map_to_the_engines():
    assert game_setup("blue", "WHITE") == ("b_blue", 1)
    assert game_setup("PLASMA", "gold") == ("b_plasma", 8)


def test_a_short_branch_run_writes_its_log_decisions_and_status(model, tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "ROOT", tmp_path)
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
    assert json.loads((tmp_path / "training" / "br" / "status.json").read_text())["phase"] == "finished"


def test_the_pull_alone_moves_the_policy_toward_its_start(model, batch):
    import copy
    from argparse import Namespace

    torch.manual_seed(1)
    start = Policy(width=32, layers=1).eval()
    policy = copy.deepcopy(model)
    check = Prepared(batch.states)
    start_logp = ppo.copy_logp(start, check)

    def away():
        with torch.no_grad():
            log_q = torch.from_numpy(start_logp) - scored(policy, check, np.arange(check.n))[0]
        return float((log_q.exp() - 1 - log_q).mean())

    before = away()
    opt = torch.optim.Adam(policy.parameters(), lr=1e-3)
    args = Namespace(batch=512, max_grad=0.5, change_sample=4096, target_change=0.05, lr=1e-3)
    stats = ppo.one_step(policy, opt, Prepared(batch.examples), batch.logp, np.zeros(len(batch.adv)),
                         {}, args, np.random.default_rng(0), check=(check, batch.state_logp),
                         pull=(start_logp, 1.0))
    assert stats["away from start"] == pytest.approx(before, rel=1e-4)
    assert away() < before


def at_first_hand(bp: BranchPlay, seal: str | None) -> dict:
    """The first main line of run "test" at its first hand, every hand card given ``seal``."""
    from jackdaw.env.game_spec import FactoredAction

    main = bp._new_main()
    bp._step(main, FactoredAction(int(ActionType.SelectBlind)))
    game = main["info"]["game"]
    snapshot = game.to_dict()
    for card in snapshot["areas"]["hand"]:
        card["seal"] = seal
    game.replace_state(snapshot)
    main["env"], main["info"] = copy_env(main["env"])
    return main


@pytest.fixture(scope="module")
def seal_minded():
    """A policy that plays its hands, its seal embedding loud enough that hiding the seals
    changes which cards it plays."""
    torch.manual_seed(5)
    model = Policy(width=32, layers=1).eval()
    with torch.no_grad():
        model.embed[6].weight.mul_(40.0)
        model.type_head.bias[int(ActionType.PlayHand)] += 30.0
    return model


def test_hiding_seals_changes_only_the_hand_and_pack_cards_seals():
    bp = BranchPlay("test", rate=0.0, mains=1)
    main = at_first_hand(bp, "Gold")
    prep = Prepared(dataset.examples_of([env_view(main["info"])]))
    hidden = prep.without_seals()
    assert (prep.cats[:-1, 6] > 0).sum() == len(main["info"]["game"].hand)
    assert (hidden.cats[:, 6] == 0).all()
    changed = np.flatnonzero((hidden.feat != prep.feat).any(0))
    assert len(changed) == 1 and (hidden.cats[:, :6] == prep.cats[:, :6]).all()


def test_a_play_with_sealed_cards_branches_on_the_seal_blind_play_once_per_blind(seal_minded):
    bp = BranchPlay("test", rate=0.0, mains=1)
    bp._pairs, bp._states, bp._finished, bp._resolved = [], [], [], []
    main = at_first_hand(bp, "Gold")
    bp.lines = [main]
    view = env_view(main["info"])
    prep = Prepared(dataset.examples_of([view]))
    greedy = act(seal_minded, prep)[0]
    blind = act(seal_minded, prep.without_seals())[0][0]
    assert greedy[0].action_type in (ActionType.PlayHand, ActionType.Discard)
    assert not branch.same_move(blind, greedy[0])

    (move, logp), = bp._sealed(seal_minded, prep, greedy, set()).values()
    assert branch.same_move(move, blind)
    assert bp._sealed(seal_minded, prep, greedy, set()) == {}  # this blind's seal branch is used
    ex, stored, _ = branch._pair_examples([(view, greedy[0], 0.0, move, logp, 1.0)], DECK, STAKE)
    with torch.no_grad():
        assert np.isclose(scored(seal_minded, Prepared(ex), np.arange(2))[0].numpy()[1], stored[1],
                          atol=1e-5)

    main["forced"].clear()
    bp._tick(seal_minded)
    (line,) = [line for line in bp.lines if line["role"] == "branch"]
    assert line["decision"]["seal"] and branch.same_move(line["force"], blind)


def test_no_seal_branch_without_sealed_cards_or_where_the_seal_changes_nothing(seal_minded):
    bp = BranchPlay("test", rate=0.0, mains=1)
    main = at_first_hand(bp, None)
    bp.lines = [main]
    prep = Prepared(dataset.examples_of([env_view(main["info"])]))
    assert bp._sealed(seal_minded, prep, act(seal_minded, prep)[0], set()) == {}
    sealed = at_first_hand(bp, "Red")
    bp.lines = [sealed]
    prep = Prepared(dataset.examples_of([env_view(sealed["info"])]))
    blind = act(seal_minded, prep.without_seals())[0]
    assert bp._sealed(seal_minded, prep, blind, set()) == {}
    assert sealed["forced"] == set()  # the occasion waits for a point where the seal matters
    assert bp._sealed(seal_minded, prep, blind, {0}) == {}


def test_seal_branches_are_tallied_apart():
    d = {"phase": "selecting_hand", "main move": "PlayHand", "drawn move": "PlayHand",
         "tied": False, "joined": False, "cut": False, "diff": 2.0, "ante diff": 1, "win diff": 0}
    counts = tally([{**d, "seal": True}, d])
    assert counts["seal"]["n"] == 1 and counts["seal"]["main better"] == 1 and counts["all"]["n"] == 2
