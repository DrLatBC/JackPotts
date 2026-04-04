"""Effect text parsing functions — extract chips/mult/xmult from joker descriptions."""

from __future__ import annotations

import re

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
# Bracketed counter at end of effect text, e.g. Yorick's "[13]" remaining discards
_BRACKET_COUNTER_PATTERN = re.compile(r'\[(\d+)\]')


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


def _get_parsed_value(joker: dict, key: str, fallback: float) -> float:
    """Get a parsed value from joker effect text, falling back to estimate.

    key: one of 'chips', 'mult', 'xmult'
    fallback: the hardcoded estimate to use if parsing fails
    """
    effect_text = joker.get("value", {}).get("effect", "")
    if not effect_text:
        return fallback
    parsed = parse_effect_value(effect_text)
    value = parsed.get(key)
    return value if value is not None else fallback


def _parse_bracket_counter(joker: dict) -> int | None:
    """Extract a bracketed counter [N] from effect text (e.g. Yorick's remaining discards)."""
    effect_text = joker.get("value", {}).get("effect", "")
    if not effect_text:
        return None
    m = _BRACKET_COUNTER_PATTERN.search(effect_text)
    return int(m.group(1)) if m else None


def _ability(joker: dict) -> dict:
    """Return the joker's ability dict from the API (empty dict if absent)."""
    return joker.get("value", {}).get("ability", {})


def _ab_chips(joker: dict, fallback: float = 0) -> float:
    """Get chip value from ability data, then text parsing, then fallback."""
    ab = _ability(joker)
    v = ab.get("chips")
    if v is not None:
        return float(v)
    return _get_parsed_value(joker, "chips", fallback)


def _ab_mult(joker: dict, fallback: float = 0) -> float:
    """Get mult value from ability data, then text parsing, then fallback."""
    ab = _ability(joker)
    v = ab.get("mult")
    if v is None:
        v = ab.get("t_mult")
    if v is not None:
        return float(v)
    return _get_parsed_value(joker, "mult", fallback)


def _ab_xmult(joker: dict, fallback: float = 1.0) -> float:
    """Get xmult value from ability data, then text parsing, then fallback."""
    ab = _ability(joker)
    v = ab.get("Xmult")
    if v is None:
        v = ab.get("x_mult")
    if v is not None:
        return float(v)
    return _get_parsed_value(joker, "xmult", fallback)
