"""Base hand-level transformations for boss blinds."""

from __future__ import annotations

import math


# Per-level chip/mult increments for each hand type (fixed in Balatro).
_HAND_LEVEL_INCREMENTS: dict[str, tuple[int, int]] = {
    "High Card":       (10, 1),
    "Pair":            (15, 1),
    "Two Pair":        (20, 1),
    "Three of a Kind": (20, 2),
    "Straight":        (30, 3),
    "Flush":           (15, 2),
    "Full House":      (25, 2),
    "Four of a Kind":  (30, 3),
    "Straight Flush":  (40, 4),
    "Five of a Kind":  (35, 3),
    "Flush House":     (40, 4),
    "Flush Five":      (40, 4),
}


def flint_halve_hand_levels(hand_levels: dict[str, dict]) -> dict[str, dict]:
    """Return a copy of hand_levels with chips and mult halved (The Flint).

    The Flint halves base chips and mult at scoring time AFTER planet level-ups:
      chips -> max(floor(chips * 0.5 + 0.5), 0)
      mult  -> max(floor(mult  * 0.5 + 0.5), 1)
    """
    halved = {}
    for hand_name, data in hand_levels.items():
        if not isinstance(data, dict):
            halved[hand_name] = data
            continue
        d = dict(data)
        if "chips" in d:
            d["chips"] = max(math.floor(d["chips"] * 0.5 + 0.5), 0)
        if "mult" in d:
            d["mult"] = max(math.floor(d["mult"] * 0.5 + 0.5), 1)
        halved[hand_name] = d
    return halved


def arm_reduce_hand_levels(hand_levels: dict[str, dict]) -> dict[str, dict]:
    """Return a copy of hand_levels with each type reduced by 1 level (The Arm).

    The Arm decreases the level of the played poker hand by 1 BEFORE scoring.
    Since any hand type could be played, we reduce all of them.  Level cannot
    go below 1.
    """
    from balatro_bot.constants import HAND_INFO

    reduced = {}
    for hand_name, data in hand_levels.items():
        if not isinstance(data, dict):
            reduced[hand_name] = data
            continue
        d = dict(data)
        level = d.get("level", 1)
        if level <= 1:
            reduced[hand_name] = d
            continue
        chip_inc, mult_inc = _HAND_LEVEL_INCREMENTS.get(hand_name, (0, 0))
        base_chips, base_mult, _ = HAND_INFO.get(hand_name, (0, 0, 0))
        if "chips" in d:
            d["chips"] = max(d["chips"] - chip_inc, base_chips)
        if "mult" in d:
            d["mult"] = max(d["mult"] - mult_inc, base_mult)
        d["level"] = level - 1
        reduced[hand_name] = d
    return reduced
