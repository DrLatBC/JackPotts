"""Low-level card accessor functions.

Both hand_evaluator and joker_effects import from here, breaking the
dependency cycle between them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from balatro_bot.constants import ALL_SUITS, RANK_CHIPS, RANK_ORDER

if TYPE_CHECKING:
    from typing import Any


def _modifier(card: dict[str, Any]) -> dict[str, Any]:
    """Return the modifier dict, handling the API returning [] for empty."""
    m = card.get("modifier", {})
    return m if isinstance(m, dict) else {}


def _state(card: dict[str, Any]) -> dict[str, Any]:
    """Return the state dict, handling the API returning [] for empty."""
    s = card.get("state", {})
    return s if isinstance(s, dict) else {}


def is_debuffed(card: dict[str, Any]) -> bool:
    return _state(card).get("debuff", False) is True


def card_rank(card: dict[str, Any]) -> str | None:
    """Return the rank character, or None for Stone/non-playing cards."""
    return card.get("value", {}).get("rank")


def card_suit(card: dict[str, Any]) -> str | None:
    """Return the suit character, or None for Stone/non-playing cards."""
    return card.get("value", {}).get("suit")


def card_suits(card: dict[str, Any]) -> set[str]:
    """Return all suits this card counts as (Wild = all four)."""
    enhancement = _modifier(card).get("enhancement")
    if enhancement == "WILD":
        return set(ALL_SUITS)
    suit = card_suit(card)
    return {suit} if suit else set()


def is_stone(card: dict[str, Any]) -> bool:
    return _modifier(card).get("enhancement") == "STONE"


def card_chip_value(card: dict[str, Any]) -> int:
    """Chips this card contributes when it scores in a played hand."""
    if is_debuffed(card):
        return 0
    if is_stone(card):
        return 50
    mod = _modifier(card)
    enhancement = mod.get("enhancement", "")
    edition = mod.get("edition", "")
    bonus = 30 if enhancement == "BONUS" else 0
    foil = 50 if edition == "FOIL" else 0
    rank = card_rank(card)
    base = RANK_CHIPS.get(rank, 0) if rank else 0
    perma = card.get("value", {}).get("perma_bonus", 0) or 0
    return base + bonus + foil + perma


def card_mult_value(card: dict[str, Any]) -> int:
    """Additive mult this card contributes when it scores in a played hand."""
    if is_debuffed(card):
        return 0
    if is_stone(card):
        return 0
    mod = _modifier(card)
    enhancement = mod.get("enhancement", "")
    edition = mod.get("edition", "")
    total = 0
    if enhancement == "MULT":
        total += 4
    if edition in ("HOLO", "HOLOGRAPHIC"):
        total += 10
    if enhancement == "LUCKY":
        total += 4
    return total


def card_xmult_value(card: dict[str, Any]) -> float:
    """Multiplicative xmult this card contributes when it scores in a played hand."""
    if is_debuffed(card):
        return 1.0
    if is_stone(card):
        return 1.0
    mod = _modifier(card)
    enhancement = mod.get("enhancement", "")
    edition = mod.get("edition", "")
    result = 1.0
    if enhancement == "GLASS":
        result *= 2.0
    if edition == "POLYCHROME":
        result *= 1.5
    return result


def rank_value(rank: str) -> int:
    return RANK_ORDER.get(rank, 0)
