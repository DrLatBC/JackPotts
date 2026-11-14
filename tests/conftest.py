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


def joker(key: str, label: str = "", rarity: int | str | None = None) -> dict:
    """Build a minimal joker dict."""
    j: dict = {"key": key, "label": label or key, "set": "JOKER", "cost": {"sell": 3}}
    if rarity is not None:
        j.setdefault("value", {})["rarity"] = rarity
    return j


def live_joker(key: str, ability: dict, label: str = "", rarity: int | str | None = None,
               state: dict | None = None) -> dict:
    """A joker as the bot sees it live: gamestate ``ability`` plus its enriched ``current``.

    ``state`` supplies the rest of the gamestate the joker's current value reads
    (``money``, ``counters``, ``cards``).
    """
    from balatro_bot.joker_effects.live_values import enrich_state

    j = joker(key, label, rarity)
    j.setdefault("value", {})["ability"] = dict(ability)
    enrich_state({**(state or {}), "jokers": {"cards": [j]}})
    return j
