"""Typed playing-card model — replaces raw dict access for cards.

Used by Snapshot for hand_cards, deck_cards, consumables, shop_cards,
vouchers, and pack_cards.  The factory ``card_from_dict`` handles the
balatrobot API quirk of returning ``[]`` instead of ``{}`` for empty
nested objects.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CardValue:
    rank: str | None = None
    suit: str | None = None
    perma_bonus: int = 0


@dataclass(frozen=True)
class CardModifier:
    enhancement: str | None = None
    edition: str | None = None
    seal: str | None = None
    edition_chips: int = 0
    edition_mult: float = 0
    edition_x_mult: float = 0
    enhancement_x_mult: float = 0


@dataclass(frozen=True)
class CardState:
    debuff: bool = False
    highlight: bool = False  # Cerulean Bell forced card


@dataclass(frozen=True)
class Card:
    id: int = 0
    key: str = ""
    set_: str = ""
    label: str = ""
    value: CardValue = field(default_factory=CardValue)
    modifier: CardModifier = field(default_factory=CardModifier)
    state: CardState = field(default_factory=CardState)
    cost: dict = field(default_factory=dict)


def _safe_dict(raw) -> dict:
    """Coerce API value to dict, handling [] for empty."""
    return raw if isinstance(raw, dict) else {}


def card_from_dict(d: dict | Card) -> Card:
    """Convert a raw API card dict into a typed Card.  Pass-through if already a Card."""
    if isinstance(d, Card):
        return d
    val = _safe_dict(d.get("value", {}))
    mod = _safe_dict(d.get("modifier", {}))
    st = _safe_dict(d.get("state", {}))

    return Card(
        id=d.get("id", 0),
        key=d.get("key", ""),
        set_=d.get("set", ""),
        label=d.get("label", ""),
        value=CardValue(
            rank=val.get("rank"),
            suit=val.get("suit"),
            perma_bonus=val.get("perma_bonus", 0) or 0,
        ),
        modifier=CardModifier(
            enhancement=mod.get("enhancement"),
            edition=mod.get("edition"),
            seal=mod.get("seal"),
            edition_chips=mod.get("edition_chips", 0) or 0,
            edition_mult=mod.get("edition_mult", 0) or 0,
            edition_x_mult=mod.get("edition_x_mult", 0) or 0,
            enhancement_x_mult=mod.get("enhancement_x_mult", 0) or 0,
        ),
        state=CardState(
            debuff=st.get("debuff", False) is True,
            highlight=st.get("highlight", False) is True,
        ),
        cost=d.get("cost") or {},
    )
