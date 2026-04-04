"""Tests for play_policy and discard_policy — extracted decision logic."""

from __future__ import annotations

import random

from balatro_bot.actions import PlayCards, DiscardCards
from balatro_bot.context import RoundContext
from balatro_bot.domain.policy.play_policy import (
    choose_winning_play,
    choose_high_value_play,
    choose_best_available,
    choose_milk_play,
)
from balatro_bot.domain.policy.discard_policy import choose_discard
from balatro_bot.domain.scoring.search import best_hand, HandCandidate
from balatro_bot.strategy import compute_strategy
from tests.conftest import card, joker


HAND_LEVELS = {
    "High Card": {"chips": 5, "mult": 1, "level": 1},
    "Pair": {"chips": 10, "mult": 2, "level": 1},
    "Two Pair": {"chips": 20, "mult": 2, "level": 1},
    "Three of a Kind": {"chips": 30, "mult": 3, "level": 1},
    "Straight": {"chips": 30, "mult": 4, "level": 1},
    "Flush": {"chips": 35, "mult": 4, "level": 1},
    "Full House": {"chips": 40, "mult": 4, "level": 1},
    "Four of a Kind": {"chips": 60, "mult": 7, "level": 1},
}


def _ctx(
    *,
    hand_cards: list[dict],
    jokers: list[dict] | None = None,
    blind_name: str = "Small Blind",
    blind_score: int = 1000,
    chips_scored: int = 0,
    hands_left: int = 4,
    discards_left: int = 3,
    hand_levels: dict | None = None,
    deck_cards: list[dict] | None = None,
    money: int = 5,
    ante: int = 1,
) -> RoundContext:
    jokers = jokers or []
    hl = hand_levels or HAND_LEVELS
    deck = deck_cards or []
    strat = compute_strategy(jokers, hl)
    return RoundContext(
        blind_score=blind_score,
        blind_name=blind_name,
        chips_scored=chips_scored,
        chips_remaining=blind_score - chips_scored,
        hands_left=hands_left,
        discards_left=discards_left,
        hand_cards=hand_cards,
        hand_levels=hl,
        jokers=jokers,
        best=best_hand(hand_cards, hl, jokers=jokers, money=money,
                       discards_left=discards_left, hands_left=hands_left),
        money=money,
        ante=ante,
        round_num=1,
        min_cards=1,
        strategy=strat,
        deck_cards=deck,
    )


# ---------------------------------------------------------------------------
# choose_winning_play
# ---------------------------------------------------------------------------

class TestChooseWinningPlay:
    def test_plays_when_best_beats_blind(self):
        hand = [card("A", "H"), card("A", "D"), card("3", "C"), card("5", "S"), card("7", "H")]
        ctx = _ctx(hand_cards=hand, blind_score=30)
        action = choose_winning_play(ctx)
        assert isinstance(action, PlayCards)
        assert "needed" in action.reason

    def test_returns_none_when_cant_beat_blind(self):
        hand = [card("2", "H"), card("3", "D"), card("5", "C"), card("7", "S"), card("9", "H")]
        ctx = _ctx(hand_cards=hand, blind_score=99999)
        action = choose_winning_play(ctx)
        assert action is None

    def test_returns_none_when_no_best_hand(self):
        ctx = _ctx(hand_cards=[])
        assert choose_winning_play(ctx) is None


# ---------------------------------------------------------------------------
# choose_high_value_play
# ---------------------------------------------------------------------------

class TestChooseHighValuePlay:
    def test_plays_on_last_hand(self):
        hand = [card("2", "H"), card("3", "D"), card("5", "C"), card("7", "S"), card("9", "H")]
        ctx = _ctx(hand_cards=hand, blind_score=99999, hands_left=1, discards_left=0)
        action = choose_high_value_play(ctx)
        assert isinstance(action, PlayCards)

    def test_defers_when_hopeless_with_discards(self):
        hand = [card("2", "H"), card("3", "D"), card("5", "C"), card("7", "S"), card("9", "H")]
        ctx = _ctx(hand_cards=hand, blind_score=99999, hands_left=4, discards_left=3)
        action = choose_high_value_play(ctx)
        assert action is None

    def test_returns_none_for_needle(self):
        hand = [card("A", "H"), card("A", "D"), card("3", "C"), card("5", "S"), card("7", "H")]
        ctx = _ctx(hand_cards=hand, blind_name="The Needle", blind_score=99999)
        action = choose_high_value_play(ctx)
        assert action is None


# ---------------------------------------------------------------------------
# choose_best_available
# ---------------------------------------------------------------------------

class TestChooseBestAvailable:
    def test_plays_best_hand(self):
        hand = [card("K", "H"), card("K", "D"), card("3", "C"), card("5", "S"), card("7", "H")]
        ctx = _ctx(hand_cards=hand, blind_score=99999, discards_left=0)
        action = choose_best_available(ctx)
        assert isinstance(action, PlayCards)
        assert "best available" in action.reason

    def test_discards_when_hand_weak_and_discards_left(self):
        hand = [card("2", "H"), card("3", "D"), card("5", "C")]
        ctx = _ctx(hand_cards=hand, blind_score=99999, discards_left=2)
        action = choose_best_available(ctx)
        # Should try to discard junk since hand < 5 cards
        assert action is not None

    def test_needle_discards_if_possible(self):
        hand = [card("2", "H"), card("3", "D"), card("5", "C"), card("7", "S"), card("9", "H")]
        ctx = _ctx(hand_cards=hand, blind_name="The Needle", blind_score=99999, discards_left=2)
        action = choose_best_available(ctx)
        assert isinstance(action, DiscardCards)
        assert "Needle" in action.reason

    def test_fallback_plays_5_highest(self):
        hand = [card("2", "H"), card("3", "D"), card("4", "C"), card("5", "S"),
                card("7", "H"), card("9", "D"), card("K", "C")]
        # Build ctx with no valid best hand by using mouth lock
        ctx = _ctx(hand_cards=hand, blind_score=99999, discards_left=0)
        ctx = RoundContext(
            blind_score=99999, blind_name="The Mouth", chips_scored=0,
            chips_remaining=99999, hands_left=1, discards_left=0,
            hand_cards=hand, hand_levels=HAND_LEVELS, jokers=[],
            best=None, money=5, ante=1, round_num=1, min_cards=1,
            strategy=compute_strategy([], HAND_LEVELS), deck_cards=[],
            mouth_locked_hand="Flush",
        )
        action = choose_best_available(ctx)
        assert isinstance(action, PlayCards)
        assert len(action.card_indices) == 5


# ---------------------------------------------------------------------------
# choose_discard
# ---------------------------------------------------------------------------

class TestChooseDiscard:
    def test_returns_none_when_no_discards(self):
        hand = [card("2", "H"), card("3", "D"), card("5", "C"), card("7", "S"), card("9", "H")]
        ctx = _ctx(hand_cards=hand, blind_score=99999, discards_left=0)
        assert choose_discard(ctx) is None

    def test_returns_none_when_hand_already_wins(self):
        hand = [card("A", "H"), card("A", "D"), card("A", "C"), card("A", "S"), card("K", "H")]
        ctx = _ctx(hand_cards=hand, blind_score=50)
        assert choose_discard(ctx) is None

    def test_respects_keep_discard_jokers(self):
        hand = [card("2", "H"), card("3", "D"), card("K", "C"), card("5", "S"), card("7", "H")]
        jokers = [joker("j_banner")]
        # Tight outlook (not hopeless), banner should block discards
        ctx = _ctx(hand_cards=hand, jokers=jokers, blind_score=300, hands_left=4)
        action = choose_discard(ctx)
        assert action is None

    def test_ignores_keep_discard_when_hopeless(self):
        random.seed(42)
        hand = [card("2", "H"), card("3", "D"), card("5", "C"), card("7", "S"), card("9", "H")]
        jokers = [joker("j_banner")]
        deck = [card(r, s) for s in "HDCS" for r in "23456789TJQKA"]
        ctx = _ctx(hand_cards=hand, jokers=jokers, blind_score=99999, hands_left=1, deck_cards=deck)
        action = choose_discard(ctx)
        # Hopeless overrides keep-discard protection
        assert action is not None


# ---------------------------------------------------------------------------
# choose_milk_play
# ---------------------------------------------------------------------------

class TestChooseMilkPlay:
    def test_returns_none_when_cant_win(self):
        hand = [card("2", "H"), card("3", "D"), card("5", "C"), card("7", "S"), card("9", "H")]
        ctx = _ctx(hand_cards=hand, blind_score=99999)
        assert choose_milk_play(ctx) is None

    def test_returns_none_against_mouth(self):
        hand = [card("A", "H"), card("A", "D"), card("A", "C"), card("3", "S"), card("5", "H")]
        jokers = [joker("j_green_joker")]
        ctx = _ctx(hand_cards=hand, jokers=jokers, blind_name="The Mouth", blind_score=30)
        assert choose_milk_play(ctx) is None

    def test_returns_none_without_scaling_jokers(self):
        hand = [card("A", "H"), card("A", "D"), card("3", "C"), card("5", "S"), card("7", "H")]
        ctx = _ctx(hand_cards=hand, blind_score=30, hands_left=4)
        assert choose_milk_play(ctx) is None


# ---------------------------------------------------------------------------
# Rule delegation tests
# ---------------------------------------------------------------------------

class TestRuleDelegation:
    """Verify that rules delegate to policy and produce identical results."""

    def test_play_winning_hand_delegates(self):
        from balatro_bot.rules.playing import PlayWinningHand
        hand = [card("A", "H"), card("A", "D"), card("3", "C"), card("5", "S"), card("7", "H")]
        state = {
            "hand": {"cards": hand},
            "jokers": {"cards": [], "limit": 5},
            "cards": {"cards": []},
            "blinds": {"small": {"status": "CURRENT", "score": 30, "name": "Small Blind"}},
            "round": {"chips": 0, "hands_left": 4, "discards_left": 3},
            "hands": HAND_LEVELS,
            "money": 5,
            "ante_num": 1,
            "round_num": 1,
        }
        action = PlayWinningHand().evaluate(state)
        assert isinstance(action, PlayCards)

    def test_discard_to_improve_delegates(self):
        from balatro_bot.rules.playing import DiscardToImprove
        hand = [card("A", "H"), card("A", "D"), card("A", "C"), card("A", "S"), card("K", "H")]
        state = {
            "hand": {"cards": hand},
            "jokers": {"cards": [], "limit": 5},
            "cards": {"cards": []},
            "blinds": {"small": {"status": "CURRENT", "score": 50, "name": "Small Blind"}},
            "round": {"chips": 0, "hands_left": 4, "discards_left": 3},
            "hands": HAND_LEVELS,
            "money": 5,
            "ante_num": 1,
            "round_num": 1,
        }
        action = DiscardToImprove().evaluate(state)
        assert action is None  # hand already wins

    def test_play_best_available_delegates(self):
        from balatro_bot.rules.playing import PlayBestAvailable
        hand = [card("2", "H"), card("3", "D"), card("5", "C"), card("7", "S"), card("9", "H")]
        state = {
            "hand": {"cards": hand},
            "jokers": {"cards": [], "limit": 5},
            "cards": {"cards": []},
            "blinds": {"small": {"status": "CURRENT", "score": 99999, "name": "Small Blind"}},
            "round": {"chips": 0, "hands_left": 1, "discards_left": 0},
            "hands": HAND_LEVELS,
            "money": 5,
            "ante_num": 1,
            "round_num": 1,
        }
        action = PlayBestAvailable().evaluate(state)
        assert isinstance(action, PlayCards)
