"""Unit tests for the Mouth pre-lock commitment repeatability model."""

from __future__ import annotations

from balatro_bot.domain.models.deck_profile import DeckProfile
from balatro_bot.domain.scoring.mouth_commit import (
    _flush_repeatability,
    _repeatability,
)


def _stock_deck_profile() -> DeckProfile:
    """A fresh 52-card deck: 13 per suit, no enhancements."""
    return DeckProfile(
        total_cards=52,
        suit_counts={"H": 13, "D": 13, "C": 13, "S": 13},
        rank_counts={r: 4 for r in "23456789TJQKA"},
    )


class TestStaticRepeatability:
    def test_pair_and_high_card_are_near_certain(self):
        dp = _stock_deck_profile()
        assert _repeatability("High Card", dp, set()) == 1.0
        assert _repeatability("Pair", dp, set()) == 1.0

    def test_rare_hands_are_low(self):
        dp = _stock_deck_profile()
        assert _repeatability("Four of a Kind", dp, set()) < 0.1
        assert _repeatability("Royal Flush", dp, set()) < 0.01

    def test_unknown_hand_fallback(self):
        dp = _stock_deck_profile()
        assert _repeatability("Wacky New Hand", dp, set()) == 0.05


class TestFlushRepeatability:
    def test_stock_deck_baseline(self):
        """On a balanced 13-per-suit deck, 5-card flush draw is ~5%."""
        dp = _stock_deck_profile()
        p = _repeatability("Flush", dp, set())
        # Stock deck: p should be small but nonzero, well under 10%
        assert 0.01 < p < 0.10

    def test_four_fingers_lifts_flush(self):
        """Four Fingers (4-card flush) ~5-8x easier than 5-card."""
        dp = _stock_deck_profile()
        p_five = _repeatability("Flush", dp, set())
        p_four = _repeatability("Flush", dp, {"j_four_fingers"})
        assert p_four > p_five * 3  # generous lower bound

    def test_flush_rich_deck(self):
        """A deck concentrated in one suit raises flush p_repeat."""
        dp = DeckProfile(
            total_cards=52,
            suit_counts={"H": 30, "D": 8, "C": 7, "S": 7},
            rank_counts={r: 4 for r in "23456789TJQKA"},
        )
        p = _repeatability("Flush", dp, set())
        # 30/52 hearts, draw 8 → P(≥5 hearts) should be high
        assert p > 0.5

    def test_flush_poor_deck(self):
        """A deck with no strong suit concentration keeps flush p_repeat low."""
        dp = DeckProfile(
            total_cards=52,
            suit_counts={"H": 10, "D": 14, "C": 14, "S": 14},
            rank_counts={r: 4 for r in "23456789TJQKA"},
        )
        p = _repeatability("Flush", dp, set())
        assert p < 0.15

    def test_smeared_combines_red_and_black(self):
        """Smeared Joker groups H+D and C+S. Flush p goes up dramatically."""
        dp = _stock_deck_profile()  # 26 red, 26 black after smeared grouping
        p_normal = _repeatability("Flush", dp, set())
        p_smeared = _repeatability("Flush", dp, {"j_smeared"})
        assert p_smeared > p_normal * 2

    def test_zero_suit_deck(self):
        """Empty / broken deck profile returns 0 (not crash)."""
        dp = DeckProfile(total_cards=0, suit_counts={})
        p = _flush_repeatability(dp, four_fingers=False, smeared=False)
        assert p == 0.0


class TestStraightRepeatability:
    def test_shortcut_lifts_straight(self):
        dp = _stock_deck_profile()
        p_base = _repeatability("Straight", dp, set())
        p_short = _repeatability("Straight", dp, {"j_shortcut"})
        assert p_short > p_base

    def test_four_fingers_lifts_straight(self):
        dp = _stock_deck_profile()
        p_base = _repeatability("Straight", dp, set())
        p_ff = _repeatability("Straight", dp, {"j_four_fingers"})
        assert p_ff > p_base
