"""Exploration: runner-up shop plans and pack picks, with logged propensities."""

from collections import Counter
from types import SimpleNamespace

import pytest

from balatro_bot.actions import NextRound, RearrangeJokers, Reroll
from balatro_bot.bot_logging import record_decision
from balatro_bot.domain.policy import exploration
from balatro_bot.domain.policy.exploration import Explorer
from balatro_bot.domain.policy.shop_evaluator import ActionPlan, _explore_plan
from balatro_bot.rules.packs import PickFromPlanetPack, _explore_pick


@pytest.fixture(autouse=True)
def reset_explorer():
    exploration.configure(0.0)
    exploration.explorer().reseed("test")
    yield
    exploration.configure(0.0)
    exploration.explorer().reseed("test")


def test_rate_zero_always_greedy():
    ex = Explorer(rate=0.0)
    assert all(ex.choose(6).idx == 0 for _ in range(200))
    assert ex.choose(6).propensity == 1.0


def test_single_option_is_greedy_even_when_exploring():
    assert Explorer(rate=1.0).choose(1) == exploration.Choice(0, "greedy", 1.0)


def test_full_rate_stays_inside_top_k():
    ex = Explorer(rate=1.0, top_k=5)
    picks = {ex.choose(9).idx for _ in range(500)}
    assert picks == {0, 1, 2, 3, 4}
    assert ex.choose(9).propensity == pytest.approx(0.2)


def test_logged_propensity_matches_pick_frequency():
    ex = Explorer(rate=0.15, top_k=5)
    ex.reseed("freq")
    n = 40_000
    counts: Counter = Counter()
    props = {}
    for _ in range(n):
        c = ex.choose(4)
        counts[c.idx] += 1
        props[c.idx] = c.propensity
    for idx, p in props.items():
        assert counts[idx] / n == pytest.approx(p, abs=0.01)
    assert props[0] == pytest.approx(0.85 + 0.15 / 4)


def test_reseed_replays_the_same_choices():
    a, b = Explorer(rate=0.5), Explorer(rate=0.5)
    a.reseed("SEED1")
    b.reseed("SEED1")
    assert [a.choose(5) for _ in range(50)] == [b.choose(5) for _ in range(50)]


def _plan(desc, value, step=None):
    return ActionPlan(steps=[step or NextRound(reason=desc)], net_value=value, description=desc)


def test_shop_never_explores_into_a_reorder():
    exploration.configure(1.0)
    reorder = _plan("reorder", 1.0, RearrangeJokers(order=[1, 0], reason="r"))
    plans = [_plan("buy A", 5.0), _plan("leave", 0.5), reorder, _plan("reroll", 0.1, Reroll(reason="r"))]
    seen = {_explore_plan(plans[0], plans)[0].description for _ in range(300)}
    assert seen == {"buy A", "leave", "reroll"}


def test_shop_greedy_reorder_is_not_explored():
    exploration.configure(1.0)
    reorder = _plan("reorder", 9.0, RearrangeJokers(order=[1, 0], reason="r"))
    best, choice = _explore_plan(reorder, [reorder, _plan("leave", 0.5)])
    assert best is reorder and choice.policy == "greedy"


def _pack_state(keys, hand=()):
    return {
        "state": "SMODS_BOOSTER_OPENED",
        "pack": {"cards": [{"key": k, "label": k} for k in keys]},
        "hand": {"cards": list(hand)},
        "jokers": {"cards": []},
        "hands": {},
    }


def test_pack_alternative_without_targets_is_left_out():
    exploration.configure(1.0)
    state = _pack_state(["c_hermit", "c_devil", "c_temperance"])
    seen = Counter()
    for _ in range(300):
        decision = {"options": [
            {"idx": 0, "score": 5.0}, {"idx": 1, "score": 4.0}, {"idx": 2, "score": 1.0},
        ], "chosen_idx": 0, "reason": "greedy"}
        idx, targets = _explore_pick(decision, state, 0)
        seen[idx] += 1
        assert targets is None
        assert decision["propensity"] == pytest.approx(0.5)
    assert set(seen) == {0, 2}


def test_pack_skip_is_not_explored():
    exploration.configure(1.0)
    decision = {"options": [{"idx": 0, "score": 1.0}], "chosen_idx": None, "reason": "skip"}
    assert _explore_pick(decision, _pack_state(["c_hermit"]), None) == (None, None)
    assert decision["policy"] == "greedy"


def test_planet_pack_explored_pick_is_recorded():
    exploration.configure(1.0)
    state = _pack_state(["c_pluto", "c_mercury", "c_jupiter"])
    for c, label in zip(state["pack"]["cards"], ["Pluto", "Mercury", "Jupiter"]):
        c["label"] = label
    rule = PickFromPlanetPack()
    for i in range(50):
        exploration.explorer().reseed(f"run{i}")
        action = rule.evaluate(state)
        d = rule.last_decision
        assert d["policy"] == "explore"
        assert action.card_index == d["chosen_idx"]
        if "greedy_idx" in d:
            assert d["greedy_idx"] != d["chosen_idx"]
            assert d["reason"].startswith("explore pick")


def test_record_carries_policy_and_rate():
    exploration.configure(0.15)
    gs = SimpleNamespace(actions_taken=3, decisions=[])
    record_decision(gs, {"kind": "shop", "chosen_idx": 0, "policy": "explore", "propensity": 0.03},
                    None, "SHOP", "next_round", {}, 10, 10)
    rec = gs.decisions[0]
    assert rec["policy"] == "explore"
    assert "policy" not in rec["detail"]
    assert rec["detail"]["propensity"] == 0.03
    assert rec["detail"]["explore_rate"] == 0.15


def test_greedy_record_has_no_rate_when_not_exploring():
    gs = SimpleNamespace(actions_taken=0, decisions=[])
    record_decision(gs, {"kind": "pack", "chosen_idx": 1}, None, "TAROT_PACK", "pack", {}, 4, 4)
    assert gs.decisions[0]["policy"] == "greedy"
    assert "explore_rate" not in (gs.decisions[0]["detail"] or {})


def test_supervisor_rejects_mixed_decks_when_exploring(monkeypatch):
    from balatro_bot import supervisor
    monkeypatch.setattr("sys.argv", ["sup", "--deck", "RED,BLUE", "--explore-instances", "2"])
    with pytest.raises(SystemExit):
        supervisor.main()


# ── Run lean ──

from balatro_bot.strategy import card_lean, compute_strategy


def _scores(strat):
    return dict(strat.preferred_hands)


def test_lean_ties_the_top_hand_and_lifts_composites():
    # Explored pick Zany Joker is on the roster and sets the lean.
    roster = [{"key": "j_jolly"}, {"key": "j_duo"}, {"key": "j_zany"}]
    exploration.explorer().set_lean(card_lean({"key": "j_zany"}))
    hands = _scores(compute_strategy(roster))
    assert hands["Pair"] == pytest.approx(7)
    assert hands["Three of a Kind"] == pytest.approx(9)
    assert hands["Full House"] == pytest.approx(8)


def test_lean_floor_applies_to_an_empty_roster():
    exploration.explorer().set_lean(card_lean({"key": "c_jupiter", "label": "Jupiter"}))
    assert _scores(compute_strategy([]))["Flush"] == pytest.approx(exploration_floor())


def exploration_floor():
    from balatro_bot.strategy import LEAN_FLOOR
    return LEAN_FLOOR


def test_no_lean_leaves_strategy_unchanged():
    roster = [{"key": "j_jolly"}, {"key": "j_duo"}]
    assert "Three of a Kind" not in _scores(compute_strategy(roster))


def test_only_the_first_lean_sticks_and_reseed_clears_it():
    ex = exploration.explorer()
    assert ex.set_lean(card_lean({"key": "j_zany"}))
    assert not ex.set_lean(card_lean({"key": "j_droll"}))
    assert ex.lean == {"hands": {"Three of a Kind": 2.0}}
    ex.reseed("next run")
    assert ex.lean is None


def test_cards_that_point_nowhere_have_no_lean():
    assert card_lean({"key": "c_hermit"}) is None
    assert card_lean({"key": "v_overstock_norm"}) is None


def test_conflicts_only_when_no_channel_overlaps():
    ex = exploration.explorer()
    ex.set_lean(card_lean({"key": "j_zany"}))
    assert ex.conflicts(card_lean({"key": "j_droll"}))
    assert not ex.conflicts(card_lean({"key": "j_half"}))
    assert not ex.conflicts(None)


def test_shop_pool_drops_plans_pointing_elsewhere():
    exploration.configure(1.0)
    exploration.explorer().set_lean(card_lean({"key": "j_zany"}))
    plans = [
        _plan("buy Wily", 5.0), _plan("buy Droll", 4.0), _plan("leave", 0.5),
    ]
    plans[0].item = {"key": "j_wily"}
    plans[1].item = {"key": "j_droll"}
    seen = {_explore_plan(plans[0], plans)[0].description for _ in range(300)}
    assert seen == {"buy Wily", "leave"}


def test_explored_pack_pick_sets_the_lean():
    exploration.configure(1.0)
    state = _pack_state(["c_pluto", "c_jupiter"])
    state["pack"]["cards"][0]["label"] = "Pluto"
    state["pack"]["cards"][1]["label"] = "Jupiter"
    for _ in range(100):
        exploration.explorer().reseed(f"r{_}")
        decision = {"options": [{"idx": 0, "score": 5.0}, {"idx": 1, "score": 1.0}],
                    "chosen_idx": 0, "reason": "greedy"}
        idx, _t = _explore_pick(decision, state, 0)
        if idx == 1:
            assert decision["lean_set"] is True
            assert exploration.explorer().lean == {"hands": {"Flush": 1.0}}
            return
    pytest.fail("explored pick never landed on the runner-up")
