from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from balatro_bot.actions import RearrangeJokers, Action
from balatro_bot.context import RoundContext
from balatro_bot.domain.policy.roster_order import describe_order, roster_order
from balatro_bot.domain.policy.shop_evaluator import ShopEvaluator

if TYPE_CHECKING:
    from typing import Any

log = logging.getLogger("balatro_bot")


class ReorderJokersForScoring:
    """Put the roster in roster order for scoring (Dagger sorts as +mult in a round)."""
    name = "reorder_jokers_for_scoring"

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        owned = state.get("jokers", {}).get("cards", [])
        if len(owned) < 2:
            return None
        # Amber Acorn reshuffles jokers every hand — reordering is futile
        if RoundContext.from_state(state).blind_name == "Amber Acorn":
            return None
        order = roster_order(owned)
        if order == list(range(len(owned))):
            return None
        return RearrangeJokers(order=order, reason=f"scoring order: {describe_order(owned, order)}")


class UnifiedShopRule:
    """Single rule that replaces all siloed shop rules.

    Delegates to ShopEvaluator which scores everything (roster, shop, packs,
    vouchers, consumables), plans a budget, enumerates composite action plans,
    and picks the best one.
    """
    name = "unified_shop"

    def __init__(self) -> None:
        self._evaluator = ShopEvaluator()

    @property
    def last_decision(self) -> dict | None:
        return self._evaluator.last_decision

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        return self._evaluator.evaluate(state)


class ContinueShopPlan:
    """Runs the next step of the shop's committed plan before any other shop rule."""
    name = "continue_shop_plan"

    def __init__(self, shop: UnifiedShopRule) -> None:
        self._evaluator = shop._evaluator

    @property
    def last_decision(self) -> dict | None:
        return self._evaluator.last_decision

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        return self._evaluator.continue_plan(state)
