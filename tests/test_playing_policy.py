from __future__ import annotations

from balatro_bot.actions import SellJoker
from balatro_bot.context import RoundContext
from balatro_bot.domain.policy.playing import choose_sell_luchador, choose_verdant_leaf_unlock
from balatro_bot.rules.playing import SellLuchador, VerdantLeafUnlock
from balatro_bot.strategy import compute_strategy
from tests.conftest import card, joker


def _ctx(
    *,
    blind_name: str,
    hand_cards: list[dict],
    jokers: list[dict],
    chips_remaining: int,
    hands_left: int = 2,
    chips_scored: int = 0,
    best_total: int = 100,
    score_discount: float = 1.0,
) -> RoundContext:
    class _Best:
        total = best_total
        card_indices = [0]
        hand_name = "High Card"

    return RoundContext(
        blind_score=chips_remaining,
        blind_name=blind_name,
        chips_scored=chips_scored,
        chips_remaining=chips_remaining,
        hands_left=hands_left,
        discards_left=3,
        hand_cards=hand_cards,
        hand_levels={},
        jokers=jokers,
        best=_Best(),
        money=5,
        ante=1,
        round_num=1,
        min_cards=1,
        strategy=compute_strategy(jokers, {}),
        deck_cards=[],
        score_discount=score_discount,
    )


def test_choose_verdant_leaf_unlock_sells_cheapest_non_protected() -> None:
    debuffed = card("A", "H")
    debuffed["state"]["debuff"] = True
    jokers = [
        {"key": "j_ride_the_bus", "label": "Ride the Bus", "cost": {"sell": 1}},
        {"key": "j_joker", "label": "Joker", "cost": {"sell": 3}},
        {"key": "j_gros_michel", "label": "Gros Michel", "cost": {"sell": 2}},
    ]
    ctx = _ctx(
        blind_name="Verdant Leaf",
        hand_cards=[debuffed],
        jokers=jokers,
        chips_remaining=1000,
    )

    action = choose_verdant_leaf_unlock(ctx)

    assert action == SellJoker(0, reason="Verdant Leaf: sell Ride the Bus to unlock debuffed cards")


def test_verdant_leaf_rule_delegates_to_policy() -> None:
    debuffed = card("A", "H")
    debuffed["state"]["debuff"] = True
    state = {
        "hand": {"cards": [debuffed]},
        "jokers": {"cards": [{"key": "j_joker", "label": "Joker", "cost": {"sell": 3}}], "limit": 5},
        "cards": {"cards": []},
        "blinds": {"small": {"status": "CURRENT", "score": 1000, "name": "Verdant Leaf"}},
        "round": {"chips": 0, "hands_left": 2, "discards_left": 3},
        "hands": {},
        "money": 5,
        "ante_num": 1,
        "round_num": 1,
    }

    action = VerdantLeafUnlock().evaluate(state)

    assert action == SellJoker(0, reason="Verdant Leaf: sell Joker to unlock debuffed cards")


def test_choose_sell_luchador_sells_when_boss_is_unwinnable() -> None:
    ctx = _ctx(
        blind_name="The Wall",
        hand_cards=[card("A", "H")],
        jokers=[joker("j_luchador", "Luchador")],
        chips_remaining=1000,
        hands_left=2,
        chips_scored=200,
        best_total=100,
    )

    action = choose_sell_luchador(ctx)

    assert action == SellJoker(0, reason="Luchador: sell to disable The Wall (projected 200 < 1000 needed)")


def test_sell_luchador_rule_delegates_to_policy() -> None:
    state = {
        "hand": {"cards": [card("A", "H")]},
        "jokers": {"cards": [joker("j_luchador", "Luchador")], "limit": 5},
        "cards": {"cards": []},
        "blinds": {"small": {"status": "CURRENT", "score": 1000, "name": "The Wall"}},
        "round": {"chips": 200, "hands_left": 2, "discards_left": 3},
        "hands": {},
        "money": 5,
        "ante_num": 1,
        "round_num": 1,
    }

    action = SellLuchador().evaluate(state)

    assert action == SellJoker(0, reason="Luchador: sell to disable The Wall (projected 32 < 800 needed)")
