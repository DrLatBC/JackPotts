from __future__ import annotations

from typing import TYPE_CHECKING

from balatro_bot.actions import SelectBlind, SkipBlind, Action

if TYPE_CHECKING:
    from typing import Any


class AlwaysSelectBlind:
    name = "always_select_blind"

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        return SelectBlind(reason="baseline: always select")


class SkipForTag:
    name = "skip_for_tag"

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        from balatro_bot.domain.policy.blind_policy import choose_skip_for_tag
        if choose_skip_for_tag(state):
            return SkipBlind(reason="skip for tag reward")
        return None
