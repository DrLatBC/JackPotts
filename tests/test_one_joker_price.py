"""Every joker decision prices against the tick's one run view (ADR 0003)."""

from __future__ import annotations

import pytest

from balatro_bot.domain.policy import shop_evaluator, shop_valuation
from balatro_bot.domain.policy.run_view import run_view
from balatro_bot.domain.policy.shop_evaluator import ShopEvaluator
from balatro_bot.rules import consumables as consumable_rules
from balatro_bot.rules.consumables import UseConsumables
from balatro_bot.rules.packs import PickFromBuffoonPack
from balatro_bot.strategy import compute_strategy
from tests.conftest import joker

HAND_LEVELS = {
    "High Card": {"chips": 5, "mult": 1, "level": 1},
    "Pair": {"chips": 10, "mult": 2, "level": 2},
    "Two Pair": {"chips": 20, "mult": 2, "level": 1},
    "Flush": {"chips": 35, "mult": 4, "level": 3},
}


def _card(rank: str, suit: str) -> dict:
    return {"key": f"{suit}_{rank}", "set": "DEFAULT", "label": f"{rank}{suit}",
            "value": {"rank": rank, "suit": suit}, "modifier": {}, "state": {}, "cost": {}}


def _joker(key: str, label: str) -> dict:
    j = joker(key, label)
    j.update({"set": "JOKER", "cost": {"buy": 5, "sell": 2}})
    return j


def _state(name: str, *, blind: str = "The Plant") -> dict:
    roster = [_joker("j_bull", "Bull"), _joker("j_banner", "Banner"), _joker("j_photograph", "Photograph")]
    return {
        "state": name,
        "money": 23,
        "ante_num": 3,
        "round_num": 8,
        "jokers": {"cards": roster, "count": 3, "limit": 6},
        "consumables": {"cards": [{"key": "c_hex", "set": "SPECTRAL", "label": "Hex", "cost": {"sell": 2}}],
                        "count": 1, "limit": 2},
        "consumeable_usage": {"c_jupiter": {"count": 2, "set": "Planet"}, "c_mars": {"count": 1, "set": "Planet"}},
        "hands": HAND_LEVELS,
        "round": {"discards_left": 1, "discards_used": 2, "hands_left": 3, "chips": 0},
        "shop": {"cards": [_joker("j_jolly", "Jolly Joker")]},
        "pack": {"cards": [_joker("j_greedy_joker", "Greedy Joker")]},
        "vouchers": {"cards": []},
        "packs": {"cards": []},
        "used_vouchers": {},
        "cards": {"cards": [_card(r, s) for r in "AKQJT98" for s in "HSDC"]},
        "hand": {"cards": []},
        "blinds": {"boss": {"name": blind, "status": "CURRENT", "score": 2000}},
        "_live_stats": None,
    }


@pytest.fixture
def priced(monkeypatch):
    """Every run view a joker was priced against, by the decision that asked."""
    seen: list[object] = []
    real = shop_valuation.joker_value

    def recording(candidate, run):
        seen.append(run)
        return real(candidate, run)

    for module in (shop_valuation, shop_evaluator, consumable_rules):
        monkeypatch.setattr(module, "joker_value", recording)
    return seen


def test_shop_pack_and_hex_price_against_the_same_run_view(priced):
    state = _state("SHOP")
    ShopEvaluator().evaluate(state)
    shop = len(priced)
    PickFromBuffoonPack().evaluate(state)
    pack = len(priced) - shop
    UseConsumables().evaluate(state)
    hex_ = len(priced) - shop - pack
    assert shop and pack and hex_
    assert all(run is run_view(state) for run in priced)


def test_the_shop_view_has_the_whole_run_and_no_boss():
    run = run_view(_state("SHOP"))
    assert run.joker_limit == 6 and run.money == 23 and run.unique_planets_used == 2
    assert run.deck_profile is not None and run.deck_profile.total_cards == 28
    assert run.discards == 1  # the shop already shows what the next round starts with
    assert run.boss is None


def test_a_round_view_has_the_boss_and_the_discards_the_round_started_with():
    run = run_view(_state("SELECTING_HAND", blind="The Flint"))
    assert run.boss == "The Flint"
    assert run.discards == 3
    assert run.hand_levels is HAND_LEVELS  # the run's own levels, not The Flint's halved ones


def test_a_disabled_boss_is_not_priced():
    state = _state("SELECTING_HAND")
    state["_boss_disabled"] = True
    assert run_view(state).boss is None


def test_a_what_if_roster_gets_its_own_strategy():
    run = run_view(_state("SHOP"))
    alone = run.with_roster(run.roster[:1])
    assert alone.roster == run.roster[:1]
    assert alone.money == run.money and alone.deck_profile is run.deck_profile
    expected = compute_strategy(list(alone.roster), HAND_LEVELS, deck_profile=run.deck_profile)
    assert repr(alone.strategy) == repr(expected)
