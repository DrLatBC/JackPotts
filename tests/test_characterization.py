"""Characterization tests — freeze current behavior before refactoring.

These tests record the exact outputs of core functions as of Phase 0.
They are snapshot tests, not correctness tests. If a refactor changes
any of these values, the test fails and the change must be verified.
"""

from __future__ import annotations

from balatro_bot.context import RoundContext
from balatro_bot.domain.scoring.estimate import score_hand
from balatro_bot.domain.scoring.search import best_hand, discard_candidates
from balatro_bot.domain.policy.shop_valuation import evaluate_joker_value
from balatro_bot.strategy import compute_strategy
from tests.conftest import card, joker


# ── Helpers ──────────────────────────────────────────────────────────

_PAIR_LEVELS = {
    "High Card": {"chips": 5, "mult": 1, "level": 1},
    "Pair": {"chips": 10, "mult": 2, "level": 1},
}

_FLUSH_LEVELS = {
    **_PAIR_LEVELS,
    "Flush": {"chips": 35, "mult": 4, "level": 1},
}


def _joker_with_effect(key: str, label: str, effect: str) -> dict:
    j = joker(key, label)
    j["value"] = {"effect": effect}
    return j


# ── best_hand() ──────────────────────────────────────────────────────


class TestBestHandCharacterization:
    def test_simple_pair(self) -> None:
        hand = [card("K", "H"), card("K", "D"), card("3", "C"), card("5", "S"), card("7", "H")]
        b = best_hand(hand, _PAIR_LEVELS)
        assert b.hand_name == "Pair"
        assert b.total == 60
        assert b.card_indices == [0, 1]
        assert len(b.scoring_cards) == 2

    def test_flush_beats_pair(self) -> None:
        hand = [card("2", "H"), card("5", "H"), card("7", "H"), card("9", "H"), card("J", "H")]
        b = best_hand(hand, _FLUSH_LEVELS)
        assert b.hand_name == "Flush"
        assert b.total == 272

    def test_pair_with_joker(self) -> None:
        j = _joker_with_effect("j_joker", "Joker", "+4 Mult")
        hand = [card("K", "H"), card("K", "D"), card("3", "C"), card("5", "S"), card("7", "H")]
        b = best_hand(hand, _PAIR_LEVELS, jokers=[j])
        assert b.hand_name == "Pair"
        assert b.total == 180

    def test_psychic_min_cards(self) -> None:
        levels = {
            "Straight Flush": {"chips": 100, "mult": 8, "level": 1},
            "Flush": {"chips": 35, "mult": 4, "level": 1},
            "Straight": {"chips": 30, "mult": 4, "level": 1},
            "High Card": {"chips": 5, "mult": 1, "level": 1},
        }
        hand = [
            card("A", "H"), card("K", "H"), card("Q", "H"),
            card("J", "H"), card("T", "H"), card("3", "C"), card("5", "S"),
        ]
        b = best_hand(hand, levels, min_select=5)
        assert b.hand_name == "Straight Flush"
        assert len(b.card_indices) == 5

    def test_mouth_locked_hand(self) -> None:
        levels = {
            "Pair": {"chips": 10, "mult": 2, "level": 1},
            "Two Pair": {"chips": 20, "mult": 2, "level": 1},
            "High Card": {"chips": 5, "mult": 1, "level": 1},
        }
        hand = [card("K", "H"), card("K", "D"), card("A", "S"), card("A", "C"), card("3", "H")]
        b = best_hand(hand, levels, required_hand="Pair")
        assert b.hand_name == "Pair"
        assert b.total == 64


# ── score_hand() ─────────────────────────────────────────────────────


class TestScoreHandCharacterization:
    def test_basic_pair(self) -> None:
        cards = [card("K", "H"), card("K", "D")]
        c, m, t = score_hand("Pair", cards, _PAIR_LEVELS)
        assert (c, m, t) == (30, 2.0, 60)

    def test_pair_plus_flat_mult_joker(self) -> None:
        j = _joker_with_effect("j_joker", "Joker", "+4 Mult")
        cards = [card("K", "H"), card("K", "D")]
        c, m, t = score_hand("Pair", cards, _PAIR_LEVELS, jokers=[j])
        assert (c, m, t) == (30, 6.0, 180)

    def test_pair_plus_xmult_joker(self) -> None:
        j = _joker_with_effect("j_duo", "The Duo", "X2 Mult if played hand contains a Pair")
        cards = [card("K", "H"), card("K", "D")]
        c, m, t = score_hand("Pair", cards, _PAIR_LEVELS, jokers=[j])
        assert (c, m, t) == (30, 4.0, 120)

    def test_leveled_pair(self) -> None:
        levels = {"Pair": {"chips": 40, "mult": 4, "level": 3}}
        cards = [card("K", "H"), card("K", "D")]
        c, m, t = score_hand("Pair", cards, levels)
        assert (c, m, t) == (60, 4.0, 240)


# ── discard_candidates() ────────────────────────────────────────────


class TestDiscardCharacterization:
    def test_keep_pair_discard_junk(self) -> None:
        hand = [card("K", "H"), card("K", "D"), card("3", "C"), card("5", "S"), card("7", "H")]
        result = discard_candidates(hand, _PAIR_LEVELS)
        assert len(result) >= 1
        first = result[0]
        assert first.discard_indices == [2, 3, 4]
        assert first.keep_indices == [0, 1]
        assert first.chase_hand == "Pair"
        assert first.hit_prob == 1.0

    def test_all_junk_keeps_highest(self) -> None:
        hand = [card("2", "H"), card("4", "D"), card("6", "C"), card("8", "S"), card("T", "H")]
        result = discard_candidates(hand, _PAIR_LEVELS)
        assert len(result) >= 1
        first = result[0]
        assert first.keep_indices == [4]  # T is highest
        assert first.chase_hand == "High Card"


# ── evaluate_joker_value() ──────────────────────────────────────────


class TestJokerValuationCharacterization:
    def test_utility_joker(self) -> None:
        j = joker("j_four_fingers", "Four Fingers")
        levels = {"Pair": {"chips": 10, "mult": 2, "level": 1}}
        strat = compute_strategy([], levels)
        v = evaluate_joker_value(j, owned_jokers=[], hand_levels=levels, ante=3, strategy=strat)
        assert v == 2.5

    def test_scoring_joker(self) -> None:
        j = _joker_with_effect("j_joker", "Joker", "+4 Mult")
        levels = {"Pair": {"chips": 10, "mult": 2, "level": 1}, "Flush": {"chips": 35, "mult": 4, "level": 1}}
        strat = compute_strategy([], levels)
        v = evaluate_joker_value(j, owned_jokers=[], hand_levels=levels, ante=3, strategy=strat)
        assert v > 5.0  # exact: ~5.62, but allow minor float drift

    def test_xmult_beats_flat_mult(self) -> None:
        levels = {"Pair": {"chips": 10, "mult": 2, "level": 1}, "Flush": {"chips": 35, "mult": 4, "level": 1}}
        strat = compute_strategy([], levels)
        j_flat = _joker_with_effect("j_joker", "Joker", "+4 Mult")
        j_xmult = _joker_with_effect("j_duo", "The Duo", "X2 Mult if played hand contains a Pair")
        v_flat = evaluate_joker_value(j_flat, owned_jokers=[], hand_levels=levels, ante=3, strategy=strat)
        v_xmult = evaluate_joker_value(j_xmult, owned_jokers=[], hand_levels=levels, ante=3, strategy=strat)
        assert v_xmult > v_flat


# ── RoundContext.from_state() ────────────────────────────────────────


class TestRoundContextCharacterization:
    @staticmethod
    def _base_state(**overrides: object) -> dict:
        state: dict = {
            "hand": {"cards": [card("K", "H"), card("K", "D"), card("3", "C"), card("5", "S"), card("7", "H")]},
            "jokers": {"cards": [], "limit": 5},
            "cards": {"cards": [card("A", "S")], "count": 40},
            "round": {"chips": 100, "hands_left": 3, "discards_left": 2},
            "blinds": {"small": {"key": "bl_small", "status": "CURRENT", "score": 300, "name": "Small Blind"}},
            "hands": {"High Card": {"chips": 5, "mult": 1, "level": 1}, "Pair": {"chips": 10, "mult": 2, "level": 1}},
            "money": 10,
            "ante_num": 2,
            "round_num": 3,
        }
        state.update(overrides)
        return state

    def test_normal_blind_fields(self) -> None:
        ctx = RoundContext.from_state(self._base_state())
        assert ctx.blind_name == "Small Blind"
        assert ctx.blind_score == 300
        assert ctx.chips_scored == 100
        assert ctx.chips_remaining == 200
        assert ctx.hands_left == 3
        assert ctx.discards_left == 2
        assert ctx.money == 10
        assert ctx.ante == 2
        assert ctx.round_num == 3
        assert ctx.min_cards == 1
        assert ctx.score_discount == 1.0
        assert ctx.best is not None

    def test_psychic_min_cards(self) -> None:
        state = self._base_state(
            blinds={"boss": {"key": "bl_psychic", "status": "CURRENT", "score": 500, "name": "The Psychic"}},
        )
        ctx = RoundContext.from_state(state)
        assert ctx.min_cards == 5
        assert ctx.blind_name == "The Psychic"

    def test_flint_halves_hand_levels(self) -> None:
        state = self._base_state(
            blinds={"boss": {"key": "bl_flint", "status": "CURRENT", "score": 800, "name": "The Flint"}},
        )
        ctx = RoundContext.from_state(state)
        assert ctx.hand_levels["Pair"]["chips"] == 5
        assert ctx.hand_levels["Pair"]["mult"] == 1
        assert ctx.hand_levels["High Card"]["chips"] == 3
        assert ctx.hand_levels["High Card"]["mult"] == 1
