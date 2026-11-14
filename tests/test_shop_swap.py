"""Tests for post-sell valuation in ShopEvaluator's sell-then-buy branch.

The sell-then-buy branch must evaluate the shop candidate against the
post-sell roster so the buyer doesn't inherit synergy from the joker
we're about to sell. Otherwise _AMPLIFICATION_PAIRS credits the
seller's enabler boost to the buyer — e.g. selling Shortcut to buy The
Order while The Order's value is still scored with Shortcut in the roster.
"""

from __future__ import annotations

from balatro_bot.domain.policy.shop_valuation import evaluate_joker_value
from balatro_bot.strategy import compute_strategy
from tests.conftest import joker


# Straight build — Crazy drives Straight into preferred_hands, Shortcut
# + Four Fingers are Straight enablers.
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
    The Order's value. The sell-then-buy branch relies on this: it scores
    the buyer against the post-sell roster, so the synergy boost disappears."""
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


# ── Swaps are carried out as one committed plan ──

from balatro_bot.actions import BuyCard, SellJoker  # noqa: E402
from balatro_bot.domain.policy.shop_evaluator import ShopEvaluator  # noqa: E402
from tests.test_decision_logging import _shop_state  # noqa: E402


def _swap_state(money: int = 40, owned_count: int = 5) -> dict:
    """Full roster of weak jokers, a Negative Duo in the shop: the evaluator swaps."""
    state = _shop_state(money=money)
    state["ante_num"] = 4
    owned = [dict(joker("j_credit_card", "Credit Card"), cost={"sell": 1}) for _ in range(owned_count)]
    state["jokers"] = {"cards": owned, "count": owned_count, "limit": 5}
    duo = joker("j_duo", "The Duo")
    duo.update({"cost": {"buy": 8}, "modifier": {"edition": "NEGATIVE"},
                "value": {"effect": "X2 Mult if played hand contains a Pair"}})
    state["shop"] = {"cards": [duo]}
    return state


def test_swap_sells_then_queues_the_buy() -> None:
    ev = ShopEvaluator()
    action = ev.evaluate(_swap_state())
    assert isinstance(action, SellJoker)
    assert ev.last_decision["candidates"][0]["steps"] == [["sell", {"joker": action.index}], ["buy", {"card": 0}]]
    assert len(ev.pending_plan) == 1 and isinstance(ev.pending_plan[0], BuyCard)


def test_swap_buy_runs_next_instead_of_being_redecided() -> None:
    ev = ShopEvaluator()
    ev.evaluate(_swap_state())
    action = ev.evaluate(_swap_state(money=41, owned_count=4))
    assert action == BuyCard(0, reason=action.reason)
    assert ev.last_decision == {"kind": "shop", "continuation": True}
    assert ev.pending_plan is None


def test_swap_buy_dropped_when_the_shop_card_changed() -> None:
    ev = ShopEvaluator()
    ev.evaluate(_swap_state())
    after = _swap_state(money=41, owned_count=4)
    junk = joker("j_credit_card", "Credit Card")
    junk.update({"cost": {"buy": 999}})
    after["shop"] = {"cards": [junk]}
    action = ev.evaluate(after)
    assert not isinstance(action, BuyCard)
    assert ev.last_decision.get("continuation") is False


def test_swap_buy_dropped_when_no_longer_affordable() -> None:
    ev = ShopEvaluator()
    ev.evaluate(_swap_state())
    action = ev.evaluate(_swap_state(money=5, owned_count=4))
    assert not isinstance(action, BuyCard)


def test_swap_never_sells_a_negative() -> None:
    """Selling a Negative takes its slot with it, so the queued buy would be refused."""
    state = _swap_state()
    owned = [dict(joker("j_credit_card", "Credit Card"), cost={"sell": 1}) for _ in range(6)]
    owned[0]["modifier"] = {"edition": "NEGATIVE"}
    state["jokers"] = {"cards": owned, "count": 6, "limit": 6}
    ev = ShopEvaluator()
    action = ev.evaluate(state)
    assert isinstance(action, SellJoker) and action.index != 0
    for cand in ev.last_decision["candidates"]:
        assert ["sell", {"joker": 0}] not in cand["steps"]


def test_swap_buy_dropped_when_the_freed_slot_was_refilled() -> None:
    ev = ShopEvaluator()
    ev.evaluate(_swap_state())
    assert ev.continue_plan(_swap_state(money=41, owned_count=5)) is None
    assert ev.pending_plan is None


def test_swap_buy_runs_before_consumables_can_fill_the_slot() -> None:
    from balatro_bot.engine import RuleEngine
    from balatro_bot.rules import ContinueShopPlan, UnifiedShopRule, UseConsumables

    shop = UnifiedShopRule()
    engine = RuleEngine({"SHOP": [ContinueShopPlan(shop), UseConsumables(), shop]})
    assert isinstance(engine.decide(_swap_state()), SellJoker)

    after = _swap_state(money=41, owned_count=4)
    judgement = {"key": "c_judgement", "label": "Judgement", "set": "TAROT", "cost": {"sell": 1}}
    after["consumables"] = {"cards": [judgement], "count": 1, "limit": 2}
    assert UseConsumables().evaluate(dict(after)) is not None, "sanity: Judgement is usable here"
    assert isinstance(engine.decide(after), BuyCard)


def test_default_shop_rules_continue_the_plan_first() -> None:
    from balatro_bot.engine import DEFAULT_RULES
    from balatro_bot.rules import ContinueShopPlan, UnifiedShopRule

    rules = DEFAULT_RULES["SHOP"]
    assert isinstance(rules[0], ContinueShopPlan) and isinstance(rules[-1], UnifiedShopRule)
    assert rules[0]._evaluator is rules[-1]._evaluator
