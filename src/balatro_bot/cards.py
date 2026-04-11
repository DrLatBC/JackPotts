"""Low-level card accessor functions.

Both hand_evaluator and joker_effects import from here, breaking the
dependency cycle between them.

Accepts both typed Card objects and raw dicts during the migration period.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from balatro_bot.constants import ALL_SUITS, RANK_CHIPS, RANK_ORDER
from balatro_bot.domain.models.card import Card
from balatro_bot.domain.models.joker import Joker

if TYPE_CHECKING:
    from typing import Any

    CardLike = Card | dict[str, Any]
    JokerLike = Joker | dict[str, Any]


def _modifier(card: CardLike) -> dict[str, Any]:
    """Return the modifier as a dict, handling Card, Joker, and raw dicts.

    For typed objects, converts CardModifier to a dict for backward compat
    with callers that use .get() on the result.
    """
    if isinstance(card, (Card, Joker)):
        mod = card.modifier
        d: dict[str, Any] = {}
        if mod.enhancement is not None:
            d["enhancement"] = mod.enhancement
        if mod.edition is not None:
            d["edition"] = mod.edition
        if mod.seal is not None:
            d["seal"] = mod.seal
        if mod.edition_chips:
            d["edition_chips"] = mod.edition_chips
        if mod.edition_mult:
            d["edition_mult"] = mod.edition_mult
        if mod.edition_x_mult:
            d["edition_x_mult"] = mod.edition_x_mult
        if mod.enhancement_x_mult:
            d["enhancement_x_mult"] = mod.enhancement_x_mult
        return d
    m = card.get("modifier", {})
    return m if isinstance(m, dict) else {}


def _state(card: CardLike) -> dict[str, Any]:
    """Return the state as a dict, handling both Card objects and raw dicts."""
    if isinstance(card, Card):
        return {"debuff": card.state.debuff} if card.state.debuff else {}
    s = card.get("state", {})
    return s if isinstance(s, dict) else {}


def is_debuffed(card: CardLike) -> bool:
    if isinstance(card, Card):
        return card.state.debuff
    return _state(card).get("debuff", False) is True


def is_joker_debuffed(joker: JokerLike) -> bool:
    """True if a joker is debuffed (e.g. by Crimson Heart).

    The API signals this via the effect text becoming
    ``"All abilities are disabled"`` and/or a state.debuff flag.
    """
    if isinstance(joker, Joker):
        if joker.state.debuff:
            return True
        return joker.value.effect == "All abilities are disabled"
    if _state(joker).get("debuff", False) is True:
        return True
    effect = joker.get("value", {}).get("effect", "")
    return isinstance(effect, str) and effect == "All abilities are disabled"


def joker_key(joker: JokerLike) -> str:
    """Return a joker's key, handling both Joker objects and raw dicts."""
    if isinstance(joker, Joker):
        return joker.key
    return joker.get("key", "")


def card_rank(card: CardLike) -> str | None:
    """Return the rank character, or None for Stone/non-playing cards."""
    if isinstance(card, Card):
        if card.modifier.enhancement == "STONE":
            return None
        return card.value.rank
    if _modifier(card).get("enhancement") == "STONE":
        return None
    return card.get("value", {}).get("rank")


def card_suit(card: CardLike) -> str | None:
    """Return the suit character, or None for Stone/non-playing cards."""
    if isinstance(card, Card):
        return card.value.suit
    return card.get("value", {}).get("suit")


def card_suits(card: CardLike, smeared: bool = False) -> set[str]:
    """Return all suits this card counts as (Wild = all four, Smeared = merged pairs)."""
    if isinstance(card, Card):
        enhancement = card.modifier.enhancement
        if enhancement == "WILD" and not card.state.debuff:
            return set(ALL_SUITS)
        if enhancement == "STONE":
            return set()
        suit = card.value.suit
    else:
        enhancement = _modifier(card).get("enhancement")
        if enhancement == "WILD" and not is_debuffed(card):
            return set(ALL_SUITS)
        if enhancement == "STONE":
            return set()
        suit = card_suit(card)
    if not suit:
        return set()
    if smeared:
        if suit in ("H", "D"):
            return {"H", "D"}
        return {"C", "S"}
    return {suit}


def is_stone(card: CardLike) -> bool:
    if isinstance(card, Card):
        return card.modifier.enhancement == "STONE"
    return _modifier(card).get("enhancement") == "STONE"


def card_chip_value(card: CardLike) -> int:
    """Chips this card contributes when it scores in a played hand."""
    if isinstance(card, Card):
        if card.state.debuff:
            return 0
        mod = card.modifier
        if mod.enhancement == "STONE":
            return 50 + card.value.perma_bonus
        bonus = 30 if mod.enhancement == "BONUS" else 0
        edition_chips = 50 if mod.edition == "FOIL" else (mod.edition_chips or 0)
        rank = card.value.rank if mod.enhancement != "STONE" else None
        base = RANK_CHIPS.get(rank, 0) if rank else 0
        return base + bonus + edition_chips + card.value.perma_bonus
    # dict fallback
    if is_debuffed(card):
        return 0
    if is_stone(card):
        perma = card.get("value", {}).get("perma_bonus", 0) or 0
        return 50 + perma
    mod = _modifier(card)
    enhancement = mod.get("enhancement", "")
    bonus = 30 if enhancement == "BONUS" else 0
    edition = mod.get("edition", "")
    edition_chips = 50 if edition == "FOIL" else (mod.get("edition_chips") or 0)
    rank = card_rank(card)
    base = RANK_CHIPS.get(rank, 0) if rank else 0
    perma = card.get("value", {}).get("perma_bonus", 0) or 0
    return base + bonus + edition_chips + perma


def card_mult_value(card: CardLike) -> float:
    """Enhancement-only additive mult (excludes edition mult).

    The game applies edition mult as a separate step AFTER enhancement xmult,
    so it must not be lumped in here.  See card_edition_mult_value().
    """
    if isinstance(card, Card):
        if card.state.debuff or card.modifier.enhancement == "STONE":
            return 0
        enhancement = card.modifier.enhancement
        total = 0.0
        if enhancement == "MULT":
            total += 4
        if enhancement == "LUCKY":
            total += 4
        return total
    # dict fallback
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


def card_edition_mult_value(card: CardLike) -> float:
    """Edition additive mult (HOLO = +10).

    Applied per card AFTER enhancement xmult in the game's scoring order:
    playing_card(chips/mult) -> enhancement(xmult) -> edition(mult) -> edition(xmult).
    """
    if isinstance(card, Card):
        if card.state.debuff or card.modifier.enhancement == "STONE":
            return 0
        mod = card.modifier
        if mod.edition in ("HOLO", "HOLOGRAPHIC"):
            return mod.edition_mult or 10
        return mod.edition_mult or 0
    # dict fallback
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


def card_xmult_value(card: CardLike) -> float:
    """Enhancement-only multiplicative xmult (excludes edition xmult).

    Edition xmult (Polychrome) is applied separately after edition mult.
    See card_edition_xmult_value().
    """
    if isinstance(card, Card):
        if card.state.debuff or card.modifier.enhancement == "STONE":
            return 1.0
        if card.modifier.enhancement == "GLASS":
            return card.modifier.enhancement_x_mult or 2.0
        return 1.0
    # dict fallback
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


def card_edition_xmult_value(card: CardLike) -> float:
    """Edition multiplicative xmult (Polychrome = x1.5).

    Applied per card AFTER edition mult in the game's scoring order.
    """
    if isinstance(card, Card):
        if card.state.debuff or card.modifier.enhancement == "STONE":
            return 1.0
        mod = card.modifier
        if mod.edition == "POLYCHROME":
            return mod.edition_x_mult or 1.5
        return mod.edition_x_mult or 1.0
    # dict fallback
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
