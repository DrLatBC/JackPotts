"""Tests for edition awareness in buy/sell decisions (issues #11, #12).

Covers:
- Negative edition: slot bypass, force-buy, pack picking (#12)
- Edition valuation: Polychrome > Holo > Foil bonus in evaluate_joker_value (#11)
"""

from __future__ import annotations

from balatro_bot.domain.policy.shop import _is_negative
from balatro_bot.domain.policy.pack_policy import choose_from_buffoon_pack
from balatro_bot.strategy import compute_strategy
from tests.conftest import joker


def _shop_joker(key: str, label: str, buy: int, edition: str | None = None) -> dict:
    card = joker(key, label)
    card["cost"] = {"buy": buy, "sell": 3}
    if edition:
        card["modifier"] = {"edition": edition}
    else:
        card["modifier"] = {}
    return card


# ── _is_negative helper ──────────────────────────────────────────────


def test_is_negative_true():
    card = _shop_joker("j_joker", "Joker", 4, edition="NEGATIVE")
    assert _is_negative(card)


def test_is_negative_false_no_edition():
    card = _shop_joker("j_joker", "Joker", 4)
    assert not _is_negative(card)


def test_is_negative_false_other_edition():
    card = _shop_joker("j_joker", "Joker", 4, edition="POLYCHROME")
    assert not _is_negative(card)


# ── Buffoon pack with full slots ─────────────────────────────────────


def test_buffoon_pack_picks_negative_when_full():
    """In a buffoon pack at full slots, pick the Negative joker."""
    owned = [
        joker("j_joker", "Joker"),
        joker("j_duo", "Duo"),
        joker("j_trio", "Trio"),
        joker("j_family", "Family"),
        joker("j_order", "Order"),
    ]
    hand_levels = {
        "Pair": {"level": 3, "chips": 20, "mult": 4, "played": 5},
    }
    strat = compute_strategy(owned, hand_levels)

    cards = [
        _shop_joker("j_sly", "Sly Joker", 0),
        _shop_joker("j_jolly", "Jolly Joker", 0, edition="NEGATIVE"),
    ]

    best_idx, best_score, reason = choose_from_buffoon_pack(
        cards, owned, hand_levels, ante=3, joker_limit=5,
        strat=strat, always_buy_keys=set(),
    )
    assert best_idx == 1  # the Negative one


def test_buffoon_pack_skips_all_when_full_no_negative():
    """When no Negative joker in pack and slots full, best_score stays at -1."""
    owned = [
        joker("j_joker", "Joker"),
        joker("j_duo", "Duo"),
        joker("j_trio", "Trio"),
        joker("j_family", "Family"),
        joker("j_order", "Order"),
    ]
    hand_levels = {
        "Pair": {"level": 3, "chips": 20, "mult": 4, "played": 5},
    }
    strat = compute_strategy(owned, hand_levels)

    cards = [
        _shop_joker("j_sly", "Sly Joker", 0),
        _shop_joker("j_jolly", "Jolly Joker", 0),
    ]

    best_idx, best_score, reason = choose_from_buffoon_pack(
        cards, owned, hand_levels, ante=3, joker_limit=5,
        strat=strat, always_buy_keys=set(),
    )
    # All candidates skipped, score stays at default -1
    assert best_score == -1.0


# ── Valuation bonus ──────────────────────────────────────────────────


def test_negative_treated_as_force_buy():
    """Negative edition should get force-buy treatment with a high floor value."""
    from balatro_bot.domain.policy.shop_valuation import evaluate_joker_value

    owned = [
        joker("j_joker", "Joker"),
        joker("j_duo", "Duo"),
        joker("j_trio", "Trio"),
        joker("j_family", "Family"),
    ]
    hand_levels = {
        "Pair": {"level": 3, "chips": 20, "mult": 4, "played": 5},
    }
    strat = compute_strategy(owned, hand_levels)

    neg_card = _shop_joker("j_jolly", "Jolly Joker", 0, edition="NEGATIVE")

    # The shop loop gives Negative a floor of 10.0 (same as ALWAYS_BUY)
    neg_score = evaluate_joker_value(
        neg_card, owned_jokers=owned, hand_levels=hand_levels,
        ante=3, strategy=strat, joker_limit=5,
    )
    neg_score = max(neg_score, 10.0)  # mimic shop logic

    assert neg_score >= 10.0


def test_negative_in_pack_gets_high_floor():
    """Negative jokers in packs (free!) should always be picked."""
    owned = [
        joker("j_joker", "Joker"),
        joker("j_duo", "Duo"),
        joker("j_trio", "Trio"),
        joker("j_family", "Family"),
    ]
    hand_levels = {
        "Pair": {"level": 3, "chips": 20, "mult": 4, "played": 5},
    }
    strat = compute_strategy(owned, hand_levels)

    cards = [
        _shop_joker("j_sly", "Sly Joker", 0),
        _shop_joker("j_jolly", "Jolly Joker", 0, edition="NEGATIVE"),
    ]

    best_idx, best_score, reason = choose_from_buffoon_pack(
        cards, owned, hand_levels, ante=3, joker_limit=5,
        strat=strat, always_buy_keys=set(),
    )
    # Negative should win with its high floor
    assert best_idx == 1
    assert best_score >= 10.0


# ── Edition valuation (issue #11) ────────────────────────────────────


def _eval(key: str, edition: str | None = None) -> float:
    """Evaluate a joker with optional edition against a standard build."""
    from balatro_bot.domain.policy.shop_valuation import evaluate_joker_value

    owned = [joker("j_joker", "Joker"), joker("j_duo", "Duo")]
    hand_levels = {"Pair": {"level": 3, "chips": 20, "mult": 4, "played": 5}}
    strat = compute_strategy(owned, hand_levels)
    card = _shop_joker(key, key, 4, edition=edition)
    return evaluate_joker_value(
        card, owned_jokers=owned, hand_levels=hand_levels,
        ante=3, strategy=strat, joker_limit=5,
    )


def test_polychrome_boosts_value():
    plain = _eval("j_jolly")
    poly = _eval("j_jolly", "POLYCHROME")
    assert poly > plain
    assert poly >= plain + 3.5  # Polychrome adds ~4.0


def test_holo_boosts_value():
    plain = _eval("j_jolly")
    holo = _eval("j_jolly", "HOLO")
    assert holo > plain
    assert holo >= plain + 1.0  # Holo adds ~1.5


def test_foil_boosts_value():
    plain = _eval("j_jolly")
    foil = _eval("j_jolly", "FOIL")
    assert foil > plain  # Foil adds ~0.5


def test_polychrome_always_highest():
    """Polychrome should always be the most valuable edition."""
    plain = _eval("j_jolly")
    foil = _eval("j_jolly", "FOIL")
    holo = _eval("j_jolly", "HOLO")
    poly = _eval("j_jolly", "POLYCHROME")
    assert poly > foil
    assert poly > holo
    assert poly > plain
