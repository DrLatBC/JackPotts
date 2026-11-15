"""Low-level card accessor functions.

Kept in its own module so domain/scoring and joker_effects can both use
these accessors without importing each other.

Each accessor accepts either a typed Card object or a raw API card dict.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from balatro_bot.constants import ALL_SUITS, FACE_RANKS, RANK_CHIPS, RANK_ORDER
from balatro_bot.domain.models.card import Card
from balatro_bot.domain.models.joker import Joker

if TYPE_CHECKING:
    from typing import Any

    CardLike = Card | dict[str, Any]
    JokerLike = Joker | dict[str, Any]


def _modifier(card: CardLike) -> dict[str, Any]:
    """Return the modifier as a dict, handling Card, Joker, and raw dicts.

    For typed objects this is the modifier's cached dict: callers must not mutate it.
    """
    if isinstance(card, (Card, Joker)):
        return card.modifier.as_dict
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
    """True if a joker is debuffed (e.g. by Crimson Heart): its state.debuff flag."""
    if isinstance(joker, Joker):
        return joker.state.debuff
    return _state(joker).get("debuff", False) is True


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
        return mod.get("edition_mult") or 10
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
        result *= mod.get("enhancement_x_mult") or 2.0
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
        return mod.get("edition_x_mult") or 1.5
    elif mod.get("edition_x_mult"):
        return mod["edition_x_mult"]
    return 1.0


def rank_value(rank: str) -> int:
    return RANK_ORDER.get(rank, 0)


# Scoring-op codes: one (code, value) pair per step a scored card applies.
OP_CHIPS = 0          # chips += value
OP_MULT = 1           # mult += value
OP_XMULT = 2          # mult *= value
OP_CHIPS_PER_RETRIGGER = 3  # chips += value * (0-based trigger number)
OP_CHANCE_XMULT = 4   # value is (xmult, odds): rolled with an rng, else its expected value

_SUIT_BIT = {"H": 1, "D": 2, "C": 4, "S": 8}
_HACK_RANKS = frozenset({"2", "3", "4", "5"})


class CardFacts:
    """Everything hand search and scoring read from one card, worked out once.

    Each field is the result of the accessor of the same meaning in this module,
    so the two can never disagree.
    """

    __slots__ = (
        "rank", "rank_val", "stone", "debuffed", "face", "hack_rank",
        "suits", "suits_smeared", "suit_mask", "suit_mask_smeared",
        "steel", "red_seal", "gold_seal", "base_ops", "cached",
    )

    def __init__(self, card: CardLike) -> None:
        self.cached = type(card) is Card  # built from a frozen card, so kept and reused
        rank = card_rank(card)
        self.rank = rank
        self.rank_val = rank_value(rank) if rank else 0
        self.stone = is_stone(card)
        self.debuffed = is_debuffed(card)
        self.face = rank in FACE_RANKS
        self.hack_rank = rank in _HACK_RANKS
        self.suits = frozenset(card_suits(card))
        self.suits_smeared = frozenset(card_suits(card, smeared=True))
        self.suit_mask = sum(_SUIT_BIT[s] for s in self.suits)
        self.suit_mask_smeared = sum(_SUIT_BIT[s] for s in self.suits_smeared)
        mod = _modifier(card)
        self.steel = mod.get("enhancement") == "STEEL"
        seal = mod.get("seal")
        self.red_seal = seal == "RED"
        self.gold_seal = seal == "GOLD"

        # The card's own scoring steps for one trigger, in the game's order:
        # chips, enhancement mult, enhancement xmult, edition mult, edition xmult.
        # Steps that change nothing are left out.
        ops = []
        chips = card_chip_value(card)
        if chips != 0 or type(chips) is not int:
            ops.append((OP_CHIPS, chips))
        mult = card_mult_value(card)
        if mult:
            ops.append((OP_MULT, mult))
        xmult = card_xmult_value(card)
        if xmult != 1.0:
            ops.append((OP_XMULT, xmult))
        ed_mult = card_edition_mult_value(card)
        if ed_mult:
            ops.append((OP_MULT, ed_mult))
        ed_xmult = card_edition_xmult_value(card)
        if ed_xmult != 1.0:
            ops.append((OP_XMULT, ed_xmult))
        self.base_ops = tuple(ops)


_FACTS_BY_VALUE: dict[tuple, CardFacts] = {}
_FACTS_MAX = 4096


def card_facts(card: CardLike) -> CardFacts:
    """The card's CardFacts.

    Typed Cards are frozen, so the facts are kept on the card, and equal cards
    (same value, modifier and debuff) share one CardFacts. Raw dicts are
    mutable: theirs are rebuilt on every call.
    """
    try:
        return card._facts
    except AttributeError:
        pass
    if type(card) is not Card:
        return CardFacts(card)
    key = (card.value, card.modifier, card.state.debuff)
    facts = _FACTS_BY_VALUE.get(key)
    if facts is None:
        if len(_FACTS_BY_VALUE) >= _FACTS_MAX:
            _FACTS_BY_VALUE.clear()
        facts = _FACTS_BY_VALUE[key] = CardFacts(card)
    card.__dict__["_facts"] = facts
    return facts
