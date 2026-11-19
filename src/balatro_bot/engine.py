"""Rule engine — evaluates priority-ordered rules per game state."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from balatro_bot.actions import Action, Rule, Wait
from balatro_bot.rules import (
    VerdantLeafUnlock, FollowRoundPlan, MilkScalingJokers, SellLuchador, PlayWinningHand,
    PlayHighValueHand, DiscardToImprove, PlayBestAvailable,
    AlwaysSelectBlind, SkipForTag,
    ReorderJokersForScoring,
    UnifiedShopRule, ContinueShopPlan,
    AlwaysCashOut,
    WaitForPackHand, SkipPackForRedCard, PickFromTarotPack, PickFromPlanetPack,
    PickFromBuffoonPack, PickFromSpectralPack, PickFromStandardPack,
    PickBestFromPack,
    UseConsumables,
)

if TYPE_CHECKING:
    from typing import Any

    from balatrobot import APIError

log = logging.getLogger("balatro_bot")


_unified_shop = UnifiedShopRule()

# Default rule sets by game state — priority order preserved exactly
DEFAULT_RULES: dict[str, list[Rule]] = {
    "SELECTING_HAND": [
        VerdantLeafUnlock(),
        UseConsumables(),
        FollowRoundPlan(),
        ReorderJokersForScoring(),
        SellLuchador(),
        PlayWinningHand(),
        DiscardToImprove(),
        PlayHighValueHand(),
        PlayBestAvailable(),
    ],
    "BLIND_SELECT": [
        SkipForTag(),
        AlwaysSelectBlind(),
    ],
    "SHOP": [
        ContinueShopPlan(_unified_shop),
        UseConsumables(),
        _unified_shop,
    ],
    "ROUND_EVAL": [
        AlwaysCashOut(),
    ],
    "TAROT_PACK": [WaitForPackHand(), SkipPackForRedCard(), PickFromTarotPack(), PickBestFromPack()],
    "PLANET_PACK": [PickFromPlanetPack(), PickBestFromPack()],
    "SPECTRAL_PACK": [WaitForPackHand(), SkipPackForRedCard(), PickBestFromPack()],
    "STANDARD_PACK": [SkipPackForRedCard(), PickFromStandardPack(), PickBestFromPack()],
    "BUFFOON_PACK": [PickFromBuffoonPack(), PickBestFromPack()],
    "SMODS_BOOSTER_OPENED": [
        WaitForPackHand(), SkipPackForRedCard(), PickFromTarotPack(), PickFromPlanetPack(),
        PickFromBuffoonPack(), PickFromSpectralPack(), PickBestFromPack(),
    ],
}


class RuleEngine:
    """Evaluates rules in priority order for the current game state."""

    def __init__(self, rules: dict[str, list[Rule]] | None = None) -> None:
        self.rules = rules or dict(DEFAULT_RULES)
        self.last_rule: Rule | None = None

    @property
    def last_decision(self) -> dict | None:
        """The shop or pack choice record of the rule that made the last decision."""
        return getattr(self.last_rule, "last_decision", None)

    def add_rule(self, game_state: str, rule: Rule, priority: int | None = None) -> None:
        if game_state not in self.rules:
            self.rules[game_state] = []
        if priority is None:
            self.rules[game_state].append(rule)
        else:
            self.rules[game_state].insert(priority, rule)

    def decide(self, state: dict[str, Any]) -> Action | None:
        state.pop("_round_ctx", None)

        game_state = state.get("state", "")
        rules = self.rules.get(game_state, [])
        self.last_rule = None

        try:
            for rule in rules:
                action = rule.evaluate(state)
                if isinstance(action, Wait):
                    return None
                if action is not None:
                    self.last_rule = rule
                    return action
        finally:
            state.pop("_round_ctx", None)

        return None

    def refused(self, game_state: str, method: str, params: dict | None, error: APIError) -> None:
        """Tell the rules the game refused an action, so they stop repeating it."""
        if method == "use" and error.name == "NOT_ALLOWED":
            blocked_idx = (params or {}).get("consumable")
            for rule in self.rules.get(game_state, []):
                if hasattr(rule, "_last_used_consumable"):
                    rule._last_used_consumable = None
                if hasattr(rule, "_blocked_consumables") and blocked_idx is not None:
                    rule._blocked_consumables.add(blocked_idx)
                    log.info("Blocked consumable index %d for this round", blocked_idx)
                    break
        if "joker slots are full" in error.message:
            for rule in self.rules.get("SHOP", []):
                evaluator = getattr(rule, "_evaluator", None)
                if evaluator is not None:
                    evaluator.slots_full = True
                    break
