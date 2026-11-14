"""Joker value accessors, and the description-text parser kept for log output.

Decisions read structured values: ``value.current`` (see live_values), then
``value.ability``. ``parse_effect_value`` only feeds logs and live-game diagnostics.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from balatro_bot.domain.models.joker import Joker

if TYPE_CHECKING:
    from typing import Any

    JokerLike = Joker | dict[str, Any]

_CHIPS_PATTERN = re.compile(r'\+(\d+(?:\.\d+)?)\s+Chips')
_MULT_PATTERN = re.compile(r'\+(\d+(?:\.\d+)?)\s+Mult')
_XMULT_PATTERN = re.compile(r'X(\d+(?:\.\d+)?)\s+Mult')
# Scaling xmult jokers show "gains X0.5 Mult... (Currently X1.5 Mult)"
# The "Currently" value is the accumulated total we actually want.
_CURRENTLY_XMULT_PATTERN = re.compile(r'Currently\s+X(\d+(?:\.\d+)?)')
# Scaling additive mult jokers show "gains +3 Mult... (Currently +21 Mult)"
# Some show shortened "(Currently +21)" without the "Mult" suffix.
_CURRENTLY_MULT_PATTERN = re.compile(r'Currently\s+\+(\d+(?:\.\d+)?)\s+Mult')
# Fallback: "Currently +N" without suffix — only used when text mentions "Mult" elsewhere
_CURRENTLY_BARE_PATTERN = re.compile(r'Currently\s+\+(\d+(?:\.\d+)?)\b')
# Scaling chip jokers show "gains +5 Chips... (Currently +37 Chips)"
_CURRENTLY_CHIPS_PATTERN = re.compile(r'Currently\s+\+(\d+(?:\.\d+)?)\s+Chips')


def parse_effect_value(effect_text: str) -> dict[str, float | None]:
    """Extract numeric scoring values from a joker's effect description text.

    The effect text comes from joker["value"]["effect"] and contains the
    joker's current state as displayed in the game UI. For scaling jokers,
    the text contains both the increment ("gains X0.5 Mult") and the current
    accumulated value ("Currently X2.0 Mult"). We prefer the "Currently" value.

    Returns dict with keys 'chips', 'mult', 'xmult' (each float or None).
    """
    if not effect_text:
        return {"chips": None, "mult": None, "xmult": None}

    result: dict[str, float | None] = {"chips": None, "mult": None, "xmult": None}

    # For chips: prefer "Currently +N Chips" (the accumulated total) over the
    # first "+N Chips" match (which may be the per-trigger increment).
    # Affects: Blue Joker, Hiker, Castle, Wee Joker, and other chip-scaling jokers.
    currently_chips_match = _CURRENTLY_CHIPS_PATTERN.search(effect_text)
    if currently_chips_match:
        result["chips"] = float(currently_chips_match.group(1))
    else:
        chips_match = _CHIPS_PATTERN.search(effect_text)
        if chips_match:
            result["chips"] = float(chips_match.group(1))

    # For xmult: prefer "Currently X..." (the accumulated total) over the
    # first "X... Mult" match (which may be the per-trigger increment).
    currently_match = _CURRENTLY_XMULT_PATTERN.search(effect_text)
    if currently_match:
        result["xmult"] = float(currently_match.group(1))
    else:
        # No "Currently" — use first X Mult match. For simple jokers
        # like Cavendish this is "X3 Mult". For decay jokers like Ramen
        # this is "X1.85 Mult" (current value) before the "-X0.01" decrement.
        xmult_match = _XMULT_PATTERN.search(effect_text)
        if xmult_match:
            result["xmult"] = float(xmult_match.group(1))

    # For additive mult: prefer "Currently +N Mult" (accumulated total) over
    # the first "+N Mult" match (which may be the per-trigger increment).
    currently_mult_match = _CURRENTLY_MULT_PATTERN.search(effect_text)
    if currently_mult_match:
        result["mult"] = float(currently_mult_match.group(1))
    else:
        # Some jokers show "Currently +N" without "Mult" suffix (e.g. Fortune Teller).
        # Only use the bare pattern if the text mentions "Mult" elsewhere.
        if "Mult" in effect_text:
            bare_match = _CURRENTLY_BARE_PATTERN.search(effect_text)
            if bare_match:
                result["mult"] = float(bare_match.group(1))
        if result["mult"] is None:
            mult_match = _MULT_PATTERN.search(effect_text)
            if mult_match:
                result["mult"] = float(mult_match.group(1))

    return result


def _current(joker: JokerLike) -> dict:
    """The joker's ``value.current`` (live_values.enrich_state), or {}."""
    if isinstance(joker, Joker):
        return joker.value.current
    value = joker.get("value")
    current = value.get("current") if isinstance(value, dict) else None
    return current if isinstance(current, dict) else {}


def current_value(joker: JokerLike, key: str, fallback: float) -> float:
    """One of the joker's current values ('chips', 'mult', 'xmult', 'dollars'), else fallback."""
    v = _current(joker).get(key)
    return float(v) if v is not None else fallback


def current_counter(joker: JokerLike) -> int | None:
    """The countdown the joker's description shows (Yorick's discards, Loyalty's hands)."""
    v = _current(joker).get("counter")
    return int(v) if v is not None else None


def _ability(joker: JokerLike) -> dict:
    """Return the joker's ability as a dict (for .get() callers)."""
    if isinstance(joker, Joker):
        from dataclasses import fields as dc_fields
        ab = joker.value.ability
        return {f.name: v for f in dc_fields(ab) if (v := getattr(ab, f.name)) is not None}
    return joker.get("value", {}).get("ability", {})


def _ab_chips(joker: JokerLike, fallback: float = 0) -> float:
    """Chip value: the joker's current value, then ability data, then fallback."""
    v = _current(joker).get("chips")
    if v is None:
        v = _ability(joker).get("chips")
    return float(v) if v is not None else fallback


def _ab_mult(joker: JokerLike, fallback: float = 0) -> float:
    """Mult value: the joker's current value, then ability data, then fallback."""
    v = _current(joker).get("mult")
    if v is None:
        ab = _ability(joker)
        v = ab.get("mult")
        if v is None:
            v = ab.get("t_mult")
    return float(v) if v is not None else fallback


def _ab_xmult(joker: JokerLike, fallback: float = 1.0) -> float:
    """Xmult value: the joker's current value, then ability data, then fallback."""
    v = _current(joker).get("xmult")
    if v is None:
        ab = _ability(joker)
        v = ab.get("Xmult")
        if v is None:
            v = ab.get("x_mult")
    return float(v) if v is not None else fallback
