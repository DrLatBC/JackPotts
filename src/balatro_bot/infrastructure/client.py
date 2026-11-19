"""The bot's balatrobot client: every gamestate it returns carries joker current values."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from balatrobot import BalatroClient as _BaseClient

from balatro_bot.joker_effects.live_values import enrich_state


# Requests that leave the game as it was.
_READ_ONLY = frozenset({"gamestate", "health", "menu"})


@dataclass
class BalatroClient(_BaseClient):
    """``balatrobot.BalatroClient`` with ``live_values.enrich_state`` applied to results."""

    # What the server's health reply said (``game_server.Health``): whether every
    # response is the settled game state, and the simulator's name and version.
    responses_settled: bool = False
    simulator: str = ""
    # While a list, every request that changed the game is added to it as
    # [method, params] once it has succeeded (balatro_bot.replays).
    recording: list | None = None

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        result = enrich_state(super().call(method, params))
        if self.recording is not None and method not in _READ_ONLY:
            self.recording.append([method, params])
        return result
