"""Tests for post-sell valuation in ShopEvaluator's sell-then-buy branch.

The sell-then-buy branch must evaluate the shop candidate against the
post-sell roster so the buyer doesn't inherit synergy from the joker
we're about to sell. Without this, _AMPLIFICATION_PAIRS credits the
seller's enabler boost to the buyer — the concrete failure was bot
selling Shortcut to buy The Order while The Order's value was still
scored with Shortcut in the roster.
"""

from __future__ import annotations

from balatro_bot.domain.policy.shop_valuation import evaluate_joker_value
from balatro_bot.strategy import compute_strategy
from tests.conftest import joker


# Straight build — Crazy drives Straight into preferred_hands, Shortcut
# + Four Fingers are Straight enablers, similar to the Game 095 failure.
ROSTER_KEYS = [
    "j_four_fingers",
    "j_shortcut",
    "j_crazy",
    "j_wily",
    "j_splash",
]
SHORTCUT_IDX = ROSTER_KEYS.index("j_shortcut")

HAND_LEVELS = {
    "Straight": {"chips": 0, "mult": 0, "level": 4},
    "Straight Flush": {"chips": 0, "mult": 0, "level": 3},
    "Three of a Kind": {"chips": 0, "mult": 0, "level": 1},
}


def _owned() -> list[dict]:
    return [joker(k, k) for k in ROSTER_KEYS]


def _value(card_key: str, owned: list[dict]) -> float:
    strat = compute_strategy(owned, HAND_LEVELS)
    return evaluate_joker_value(
        joker(card_key, card_key),
        owned_jokers=owned,
        hand_levels=HAND_LEVELS,
        ante=4,
        strategy=strat,
        joker_limit=5,
    )


def test_order_value_drops_when_shortcut_removed() -> None:
    """Removing Shortcut (an enabler amplification pair) should collapse
    The Order's value. This is the core mechanism the fix relies on:
    the sell-then-buy branch now scores the buyer against the post-sell
    roster, so the synergy boost disappears correctly."""
    owned = _owned()
    pre = _value("j_order", owned)

    post_sell = owned[:SHORTCUT_IDX] + owned[SHORTCUT_IDX + 1:]
    post = _value("j_order", post_sell)

    assert pre > 0, f"sanity: pre-sell value should be nonzero (got {pre:.2f})"
    assert post < pre, (
        f"The Order should lose value once Shortcut is gone "
        f"(pre={pre:.2f}, post={post:.2f})"
    )
    # Material drop, not just rounding noise
    assert (pre - post) >= 1.0, (
        f"Enabler removal should meaningfully reduce buyer's value "
        f"(pre={pre:.2f}, post={post:.2f})"
    )


def test_non_enabler_sell_preserves_order_value() -> None:
    """Control: removing a non-enabler, non-competing joker (Splash)
    should not meaningfully shift The Order's value. Guards against the
    test above being a false positive from any arbitrary roster change."""
    owned = _owned()
    pre = _value("j_order", owned)

    splash_idx = ROSTER_KEYS.index("j_splash")
    post_sell = owned[:splash_idx] + owned[splash_idx + 1:]
    post = _value("j_order", post_sell)

    # Splash isn't in any _AMPLIFICATION_PAIRS row with The Order, doesn't
    # add scoring to Straight, so removing it shouldn't shift the buyer's value.
    # Tolerance scales with pre so this stays stable as synergy signals evolve.
    # (Splash does still affect the scoring sim via its "all cards score"
    # mechanic, so a small residual delta is expected.)
    assert abs(pre - post) / max(pre, 1.0) < 0.05, (
        f"Removing a non-enabler should not meaningfully shift The Order's "
        f"value (pre={pre:.2f}, post={post:.2f})"
    )
