"""Typed model for hand level data (e.g. Pair lv3: 20 chips, 12 mult)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class HandLevel:
    """Immutable snapshot of a single hand type's level data."""

    chips: int = 0
    mult: int = 0
    level: int = 1
    played: int = 0
    played_this_round: int = 0

    def get(self, key: str, default=None):
        """Dict-compatible .get() for backward compat with callers chaining .get()."""
        return getattr(self, key, default)

    def __contains__(self, key: str) -> bool:
        return hasattr(self, key)

    def __getitem__(self, key: str):
        try:
            return getattr(self, key)
        except AttributeError:
            raise KeyError(key)


def hand_level_from_dict(d: dict | HandLevel) -> HandLevel:
    """Convert a raw API hand-level dict to a HandLevel object."""
    if isinstance(d, HandLevel):
        return d
    if not isinstance(d, dict):
        return HandLevel()
    return HandLevel(
        chips=d.get("chips", 0),
        mult=d.get("mult", 0),
        level=d.get("level", 1),
        played=d.get("played", 0),
        played_this_round=d.get("played_this_round", 0),
    )


def hand_levels_from_dict(raw: dict) -> dict[str, HandLevel]:
    """Convert the full hands dict {hand_name: {chips, mult, ...}} to typed form."""
    return {
        name: hand_level_from_dict(data)
        for name, data in raw.items()
        if isinstance(data, dict)
    }
