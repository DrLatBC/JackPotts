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
    if _modifier(card).get("enhancement") == "STONE":
        return None
    return card.get("value", {}).get("rank")


def card_suit(card: dict[str, Any]) -> str | None:
    """Return the suit character, or None for Stone/non-playing cards."""
    return card.get("value", {}).get("suit")


def card_suits(card: dict[str, Any], smeared: bool = False) -> set[str]:
    """Return all suits this card counts as (Wild = all four, Smeared = merged pairs)."""
    enhancement = _modifier(card).get("enhancement")
    if enhancement == "WILD" and not is_debuffed(card):
        return set(ALL_SUITS)
    if enhancement == "STONE":
        return set()  # Stone cards have no suit
    suit = card_suit(card)
    if not suit:
        return set()
    if smeared:
        # Hearts and Diamonds merge; Clubs and Spades merge
        if suit in ("H", "D"):
            return {"H", "D"}
        return {"C", "S"}
    return {suit}


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
    bonus = 30 if enhancement == "BONUS" else 0
    # Edition chips: use hardcoded value for known editions (API fields are unreliable)
    edition = mod.get("edition", "")
    edition_chips = 50 if edition == "FOIL" else (mod.get("edition_chips") or 0)
    rank = card_rank(card)
    base = RANK_CHIPS.get(rank, 0) if rank else 0
    perma = card.get("value", {}).get("perma_bonus", 0) or 0
    return base + bonus + edition_chips + perma


def card_mult_value(card: dict[str, Any]) -> float:
    """Enhancement-only additive mult (excludes edition mult).

    The game applies edition mult as a separate step AFTER enhancement xmult,
    so it must not be lumped in here.  See card_edition_mult_value().
    """
    if is_debuffed(card):
        return 0
    if is_stone(card):
        return 0
    mod = _modifier(card)
    enhancement = mod.get("enhancement", "")
    total = 0.0
    if enhancement == "MULT":
        total += 4
    if enhancement == "LUCKY":
        total += 4
    return total


def card_edition_mult_value(card: dict[str, Any]) -> float:
    """Edition additive mult (HOLO = +10).

    Applied per card AFTER enhancement xmult in the game's scoring order:
    playing_card(chips/mult) → enhancement(xmult) → edition(mult) → edition(xmult).
    """
    if is_debuffed(card):
        return 0
    if is_stone(card):
        return 0
    mod = _modifier(card)
    edition = mod.get("edition", "")
    if edition in ("HOLO", "HOLOGRAPHIC"):
        return mod.get("edition_mult", 10)
    elif mod.get("edition_mult"):
        return mod["edition_mult"]
    return 0


def card_xmult_value(card: dict[str, Any]) -> float:
    """Enhancement-only multiplicative xmult (excludes edition xmult).

    Edition xmult (Polychrome) is applied separately after edition mult.
    See card_edition_xmult_value().
    """
    if is_debuffed(card):
        return 1.0
    if is_stone(card):
        return 1.0
    mod = _modifier(card)
    enhancement = mod.get("enhancement", "")
    result = 1.0
    if enhancement == "GLASS":
        result *= mod.get("enhancement_x_mult", 2.0)
    return result


def card_edition_xmult_value(card: dict[str, Any]) -> float:
    """Edition multiplicative xmult (Polychrome = x1.5).

    Applied per card AFTER edition mult in the game's scoring order.
    """
    if is_debuffed(card):
        return 1.0
    if is_stone(card):
        return 1.0
    mod = _modifier(card)
    if mod.get("edition") == "POLYCHROME":
        return mod.get("edition_x_mult", 1.5)
    elif mod.get("edition_x_mult"):
        return mod["edition_x_mult"]
    return 1.0


def rank_value(rank: str) -> int:
    return RANK_ORDER.get(rank, 0)
