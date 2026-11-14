"""The bot's balatrobot client: every gamestate it returns carries joker current values."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from balatrobot import BalatroClient as _BaseClient

from balatro_bot.joker_effects.live_values import enrich_state


@dataclass
class BalatroClient(_BaseClient):
    """``balatrobot.BalatroClient`` with ``live_values.enrich_state`` applied to results."""

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return enrich_state(super().call(method, params))
