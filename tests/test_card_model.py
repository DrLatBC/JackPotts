"""Tests for the Card typed model and card_from_dict factory."""

from balatro_bot.domain.models.card import (
    Card, CardModifier, CardState, CardValue, card_from_dict,
)


def test_card_from_dict_full():
    d = {
        "id": 42,
        "key": "H_K",
        "set": "DEFAULT",
        "label": "King of Hearts",
        "value": {"rank": "K", "suit": "H", "perma_bonus": 5},
        "modifier": {
            "enhancement": "GLASS",
            "edition": "POLYCHROME",
            "seal": "RED",
            "edition_chips": 0,
            "edition_mult": 0,
            "edition_x_mult": 1.5,
            "enhancement_x_mult": 2.0,
        },
        "state": {"debuff": True},
        "cost": {"buy": 3, "sell": 1},
    }
    c = card_from_dict(d)
    assert c.id == 42
    assert c.key == "H_K"
    assert c.set_ == "DEFAULT"
    assert c.label == "King of Hearts"
    assert c.value.rank == "K"
    assert c.value.suit == "H"
    assert c.value.perma_bonus == 5
    assert c.modifier.enhancement == "GLASS"
    assert c.modifier.edition == "POLYCHROME"
    assert c.modifier.seal == "RED"
    assert c.modifier.edition_x_mult == 1.5
    assert c.modifier.enhancement_x_mult == 2.0
    assert c.state.debuff is True
    assert c.cost == {"buy": 3, "sell": 1}


def test_card_from_dict_minimal():
    d = {"key": "S_A"}
    c = card_from_dict(d)
    assert c.key == "S_A"
    assert c.id == 0
    assert c.value.rank is None
    assert c.modifier.enhancement is None
    assert c.state.debuff is False


def test_card_from_dict_empty_list_quirk():
    """The balatrobot API returns [] instead of {} for empty nested objects."""
    d = {
        "key": "H_3",
        "value": {"rank": "3", "suit": "H"},
        "modifier": [],
        "state": [],
    }
    c = card_from_dict(d)
    assert c.modifier.enhancement is None
    assert c.state.debuff is False
    assert c.value.rank == "3"


def test_card_from_dict_perma_bonus_none():
    """perma_bonus can be None in the API."""
    d = {"key": "H_5", "value": {"rank": "5", "suit": "H", "perma_bonus": None}}
    c = card_from_dict(d)
    assert c.value.perma_bonus == 0


def test_card_frozen():
    c = card_from_dict({"key": "H_A", "value": {"rank": "A", "suit": "H"}})
    try:
        c.key = "changed"
        assert False, "Should have raised FrozenInstanceError"
    except AttributeError:
        pass


def test_card_replace():
    """dataclasses.replace works for before-phase mutation pattern."""
    from dataclasses import replace
    c = card_from_dict({
        "key": "H_K",
        "value": {"rank": "K", "suit": "H"},
        "modifier": {"enhancement": "BONUS"},
    })
    c2 = replace(c, modifier=replace(c.modifier, enhancement="GOLD"))
    assert c.modifier.enhancement == "BONUS"
    assert c2.modifier.enhancement == "GOLD"
    assert c is not c2
