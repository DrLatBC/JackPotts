"""The bot's balatrobot client: every gamestate it returns carries joker current values."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from balatrobot import BalatroClient as _BaseClient

from balatro_bot.joker_effects.live_values import enrich_state


@dataclass
class BalatroClient(_BaseClient):
    """``balatrobot.BalatroClient`` with ``live_values.enrich_state`` applied to results."""

    # True when every response is the settled game state (the simulator says so in
    # ``health``). The live game answers before money, joker counters and debuff
    # flags settle, so the bot re-polls before each hand decision there.
    responses_settled: bool = False

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return enrich_state(super().call(method, params))
