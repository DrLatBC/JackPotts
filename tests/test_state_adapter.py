"""Tests for the internal state adapter seam."""

from balatro_bot.context import RoundContext
from balatro_bot.infrastructure.state_adapter import adapt_state
from tests.conftest import card, joker


def _make_state() -> dict:
    return {
        "state": "SELECTING_HAND",
        "seed": "ABC123",
        "ante_num": 2,
        "round_num": 5,
        "money": 17,
        "hand": {"cards": [
            card("K", "H"), card("K", "D"), card("3", "C"), card("5", "S"), card("7", "H"),
        ]},
        "jokers": {"cards": [joker("j_duo")], "limit": 6},
        "cards": {"cards": [card("A", "S")], "count": 33},
        "round": {"chips": 120, "hands_left": 2, "discards_left": 1, "ancient_suit": "D"},
        "blinds": {
            "boss": {"key": "bl_psychic", "status": "CURRENT", "score": 500, "name": "The Psychic"},
        },
        "hands": {
            "High Card": {"chips": 5, "mult": 1, "level": 1},
            "Pair": {"chips": 10, "mult": 2, "level": 1},
        },
    }


def test_adapt_state_extracts_current_blind_and_round_fields():
    snapshot = adapt_state(_make_state())

    assert snapshot.state_name == "SELECTING_HAND"
    assert snapshot.seed == "ABC123"
    assert snapshot.ante == 2
    assert snapshot.round_num == 5
    assert snapshot.money == 17
    assert snapshot.joker_limit == 6
    assert snapshot.deck_count == 33
    assert snapshot.round.chips == 120
    assert snapshot.round.hands_left == 2
    assert snapshot.round.discards_left == 1
    assert snapshot.round.ancient_suit == "D"
    assert snapshot.current_blind.key == "bl_psychic"
    assert snapshot.current_blind.name == "The Psychic"
    assert snapshot.current_blind.score == 500


def test_round_context_from_snapshot_matches_round_rules():
    ctx = RoundContext.from_snapshot(adapt_state(_make_state()))

    assert ctx.blind_name == "The Psychic"
    assert ctx.blind_score == 500
    assert ctx.chips_scored == 120
    assert ctx.chips_remaining == 380
    assert ctx.min_cards == 5
    assert ctx.ante == 2
    assert ctx.round_num == 5
    assert ctx.ancient_suit == "D"
    assert ctx.best is not None
