"""Shared test fixtures — card factory helpers."""

from balatro_bot.domain.models.card import Card, CardModifier, CardState, CardValue


def card(rank: str, suit: str, enhancement: str | None = None) -> Card:
    """Build a minimal Card matching the balatrobot schema."""
    return Card(
        id=0,
        key=f"{suit}_{rank}",
        set_="DEFAULT",
        label=f"{rank} of {suit}",
        value=CardValue(rank=rank, suit=suit),
        modifier=CardModifier(enhancement=enhancement),
        state=CardState(),
        cost={},
    )


def stone_card() -> Card:
    return Card(
        id=0, key="stone", set_="ENHANCED", label="Stone Card",
        value=CardValue(), modifier=CardModifier(enhancement="STONE"),
        state=CardState(), cost={},
    )


def wild_card(rank: str, suit: str) -> Card:
    return Card(
        id=0,
        key=f"{suit}_{rank}",
        set_="DEFAULT",
        label=f"{rank} of {suit}",
        value=CardValue(rank=rank, suit=suit),
        modifier=CardModifier(enhancement="WILD"),
        state=CardState(),
        cost={},
    )


def debuffed_card(rank: str, suit: str, enhancement: str | None = None) -> Card:
    """Build a debuffed Card."""
    return Card(
        id=0,
        key=f"{suit}_{rank}",
        set_="DEFAULT",
        label=f"{rank} of {suit}",
        value=CardValue(rank=rank, suit=suit),
        modifier=CardModifier(enhancement=enhancement),
        state=CardState(debuff=True),
        cost={},
    )


def card_with_perma(rank: str, suit: str, perma_bonus: int, enhancement: str | None = None) -> Card:
    """Build a Card with perma_bonus."""
    return Card(
        id=0,
        key=f"{suit}_{rank}",
        set_="DEFAULT",
        label=f"{rank} of {suit}",
        value=CardValue(rank=rank, suit=suit, perma_bonus=perma_bonus),
        modifier=CardModifier(enhancement=enhancement),
        state=CardState(),
        cost={},
    )


def joker(key: str, label: str = "", rarity: int | str | None = None) -> dict:
    """Build a minimal joker dict."""
    j: dict = {"key": key, "label": label or key, "set": "JOKER", "cost": {"sell": 3}}
    if rarity is not None:
        j.setdefault("value", {})["rarity"] = rarity
    return j
