"""Shop plan producers, seen through ShopEvaluator.ranked_plans."""

from __future__ import annotations

import pytest

from balatro_bot.actions import SellJoker
from balatro_bot.domain.policy import shop_evaluator
from balatro_bot.domain.policy.shop_evaluator import ShopEvaluator
from tests.conftest import joker
from tests.test_decision_logging import _shop_state

WORTH: dict[str, float] = {}


@pytest.fixture(autouse=True)
def priced(monkeypatch):
    """Joker values come from WORTH (default 5.0), so each test sets the prices it needs."""
    WORTH.clear()
    monkeypatch.setattr(shop_evaluator, "joker_value", lambda card, run: WORTH.get(card["key"], 5.0))


def _owned(*keys: str, sell: int = 2) -> list[dict]:
    return [dict(joker(k, k), cost={"sell": sell}) for k in keys]


def _state(owned: list[dict], *, money: int = 12, ante: int = 1, round_num: int = 1,
           limit: int = 5, shop: list[dict] | None = None) -> dict:
    state = _shop_state(money=money)
    state.update(ante_num=ante, round_num=round_num)
    state["jokers"] = {"cards": owned, "count": len(owned), "limit": limit}
    state["shop"] = {"cards": shop if shop is not None else []}
    return state


def _kinds(state: dict, ev: ShopEvaluator | None = None) -> list[str]:
    return [p.kind for p in (ev or ShopEvaluator()).ranked_plans(state)]


def _shop_joker(key: str, buy: int = 4) -> dict:
    return dict(joker(key, key), cost={"buy": buy, "sell": buy // 2})


def test_leaving_is_always_a_plan():
    assert "leave" in _kinds(_state([]))


def test_plans_are_ranked_best_first():
    plans = ShopEvaluator().ranked_plans(_state(_owned("j_joker"), shop=[_shop_joker("j_duo")]))
    values = [p.net_value for p in plans]
    assert values == sorted(values, reverse=True)


def test_ranking_changes_nothing_on_the_evaluator():
    ev = ShopEvaluator()
    state = _state(_owned("j_invisible", "j_joker"), round_num=4)
    ev.ranked_plans(state)
    assert ev._invisible_first_seen is None and ev.pending_plan is None
    assert ev.last_decision is None and ev._rerolls_this_shop == 0


# ── Invisible Joker ──

def _invisible_state(round_num: int, *others: str) -> dict:
    WORTH.update(j_invisible=0.5, j_stuntman=8.0, j_credit_card=0.5)
    return _state(_owned("j_invisible", "j_stuntman", *others), round_num=round_num)


def test_invisible_waits_two_rounds_from_first_seen():
    ev = ShopEvaluator()
    ev.evaluate(_invisible_state(1, "j_credit_card"))
    assert ev._invisible_first_seen == 1
    assert "invisible_selldown" not in _kinds(_invisible_state(2, "j_credit_card"), ev)
    assert "invisible_selldown" in _kinds(_invisible_state(3, "j_credit_card"), ev)


def test_invisible_sells_down_the_weakest_then_sells_itself():
    ev = ShopEvaluator()
    ev.evaluate(_invisible_state(1, "j_credit_card"))
    sell_down = next(p for p in ev.ranked_plans(_invisible_state(3, "j_credit_card"))
                     if p.kind == "invisible_selldown")
    assert sell_down.steps == [SellJoker(2, reason=sell_down.steps[0].reason)]

    ev.evaluate(_invisible_state(3, "j_credit_card"))
    assert ev._invisible_selling_down is True
    plans = ev.ranked_plans(_invisible_state(3))
    assert plans[0].kind == "invisible_dupe" and plans[0].steps[0].index == 0


def test_invisible_wont_start_selling_down_before_the_boss():
    ev = ShopEvaluator()
    ev.evaluate(_invisible_state(2, "j_credit_card"))
    assert "invisible_selldown" not in _kinds(_invisible_state(5, "j_credit_card"), ev)


def test_invisible_memory_clears_when_it_leaves_the_roster():
    ev = ShopEvaluator()
    ev.evaluate(_invisible_state(1, "j_credit_card"))
    ev.evaluate(_state(_owned("j_stuntman"), round_num=2))
    assert ev._invisible_first_seen is None and ev._invisible_selling_down is False


# ── Packs and vouchers ──

def test_buffoon_pack_needs_a_free_slot_or_red_card():
    buffoon = {"label": "Buffoon Pack", "cost": {"buy": 4}}
    full = _state(_owned(*["j_joker"] * 5), ante=3, money=20)
    full["packs"] = {"cards": [buffoon]}
    assert "pack" not in _kinds(full)
    open_slot = _state(_owned(*["j_joker"] * 4), ante=3, money=20)
    open_slot["packs"] = {"cards": [buffoon]}
    assert "pack" in _kinds(open_slot)


def test_voucher_is_offered_only_when_affordable():
    state = _state(_owned("j_joker"), ante=4, money=12)
    state["vouchers"] = {"cards": [{"key": "v_grabber", "label": "Grabber", "cost": {"buy": 10}}]}
    assert "voucher" in _kinds(state)
    state["money"] = 9
    assert "voucher" not in _kinds(state)


# ── Consumables and Campfire ──

def test_no_consumable_buys_with_full_consumable_slots():
    pluto = {"key": "c_pluto", "label": "Pluto", "set": "PLANET", "cost": {"buy": 3}}
    state = _state(_owned("j_joker"), shop=[pluto])
    assert "consumable" in _kinds(state)
    state["consumables"] = {"cards": [], "count": 2, "limit": 2}
    assert "consumable" not in _kinds(state)


def test_campfire_feeds_on_an_off_build_planet():
    state = _state(_owned("j_campfire"))
    state["consumables"] = {"cards": [{"key": "c_eris", "label": "Eris", "set": "PLANET"}],
                            "count": 1, "limit": 2}
    assert "campfire" not in _kinds(_state(_owned("j_joker")))
    assert "campfire" in _kinds(state)


# ── Diet Cola, reroll, decay sells ──

def test_diet_cola_is_kept_when_the_shop_has_a_strong_joker():
    WORTH.update(j_duo=7.0, j_jolly=1.0)
    assert "diet_cola" in _kinds(_state(_owned("j_diet_cola"), shop=[_shop_joker("j_jolly")]))
    assert "diet_cola" not in _kinds(_state(_owned("j_diet_cola"), shop=[_shop_joker("j_duo")]))


def _reroll(state: dict) -> float:
    return next(p.net_value for p in ShopEvaluator().ranked_plans(state) if p.kind == "reroll")


def test_reroll_is_worth_less_when_a_good_buy_is_already_on_offer():
    WORTH.update(j_duo=9.0, j_jolly=0.5)
    poor = _reroll(_state(_owned("j_joker"), ante=6, money=30, shop=[_shop_joker("j_jolly")]))
    rich = _reroll(_state(_owned("j_joker"), ante=6, money=30, shop=[_shop_joker("j_duo")]))
    assert rich < poor


def test_reroll_stops_at_the_cap():
    ev = ShopEvaluator()
    state = _state(_owned("j_joker"), ante=6, money=30)
    ev._last_round = state["round_num"]
    ev._rerolls_this_shop = 3
    assert "reroll" not in _kinds(state, ev)


def test_decayed_popcorn_is_sold():
    WORTH.update(j_popcorn=0.0)
    popcorn = dict(joker("j_popcorn", "Popcorn"), cost={"sell": 1}, value={"ability": {"mult": 4}})
    fresh = dict(popcorn, value={"ability": {"mult": 12}})
    assert "decay_sell" in _kinds(_state([popcorn]))
    assert "decay_sell" not in _kinds(_state([fresh]))
