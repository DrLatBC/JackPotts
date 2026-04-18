"""Rule engine — evaluates priority-ordered rules per game state."""

from __future__ import annotations

from typing import TYPE_CHECKING

from balatro_bot.actions import Action, Rule
from balatro_bot.rules import (
    VerdantLeafUnlock, FollowRoundPlan, MilkScalingJokers, SellLuchador, PlayWinningHand,
    PlayHighValueHand, DiscardToImprove, PlayBestAvailable,
    AlwaysSelectBlind, SkipForTag,
    ReorderJokersForScoring,
    UnifiedShopRule,
    AlwaysCashOut,
    SkipPackForRedCard, PickFromTarotPack, PickFromPlanetPack,
    PickFromBuffoonPack, PickFromSpectralPack, PickFromStandardPack,
    PickBestFromPack,
    UseConsumables,
)

if TYPE_CHECKING:
    from typing import Any


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
        UseConsumables(),
        UnifiedShopRule(),
    ],
    "ROUND_EVAL": [
        AlwaysCashOut(),
    ],
    "TAROT_PACK": [SkipPackForRedCard(), PickFromTarotPack(), PickBestFromPack()],
    "PLANET_PACK": [PickFromPlanetPack(), PickBestFromPack()],
    "SPECTRAL_PACK": [SkipPackForRedCard(), PickBestFromPack()],
    "STANDARD_PACK": [SkipPackForRedCard(), PickFromStandardPack(), PickBestFromPack()],
    "BUFFOON_PACK": [PickFromBuffoonPack(), PickBestFromPack()],
    "SMODS_BOOSTER_OPENED": [
        SkipPackForRedCard(), PickFromTarotPack(), PickFromPlanetPack(),
        PickFromBuffoonPack(), PickFromSpectralPack(), PickBestFromPack(),
    ],
}


class RuleEngine:
    """Evaluates rules in priority order for the current game state."""

    def __init__(self, rules: dict[str, list[Rule]] | None = None) -> None:
        self.rules = rules or dict(DEFAULT_RULES)

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

        try:
            for rule in rules:
                action = rule.evaluate(state)
                if action is not None:
                    return action
        finally:
            state.pop("_round_ctx", None)

        return None
