"""Unit tests for the Mouth pre-lock commitment repeatability + chase model."""

from __future__ import annotations

import random

import pytest

from balatro_bot.domain.models.deck_profile import DeckProfile
from balatro_bot.domain.scoring.mouth_commit import (
    _flush_repeatability,
    _repeatability,
    choose_mouth_commit,
)
from tests.conftest import card


# Stable rank/chip table matching real hand levels at level 1.
_HAND_LEVELS = {
    "High Card":        {"chips": 5,   "mult": 1, "level": 1},
    "Pair":             {"chips": 10,  "mult": 2, "level": 1},
    "Two Pair":         {"chips": 20,  "mult": 2, "level": 1},
    "Three of a Kind":  {"chips": 30,  "mult": 3, "level": 1},
    "Straight":         {"chips": 30,  "mult": 4, "level": 1},
    "Flush":            {"chips": 35,  "mult": 4, "level": 1},
    "Full House":       {"chips": 40,  "mult": 4, "level": 1},
    "Four of a Kind":   {"chips": 60,  "mult": 7, "level": 1},
    "Straight Flush":   {"chips": 100, "mult": 8, "level": 1},
    "Royal Flush":      {"chips": 100, "mult": 8, "level": 1},
    "Five of a Kind":   {"chips": 120, "mult": 12, "level": 1},
    "Flush House":      {"chips": 140, "mult": 14, "level": 1},
    "Flush Five":       {"chips": 160, "mult": 16, "level": 1},
}


def _full_deck_profile(suit_counts=None) -> DeckProfile:
    suit_counts = suit_counts or {"H": 13, "D": 13, "C": 13, "S": 13}
    total = sum(suit_counts.values())
    return DeckProfile(
        total_cards=total,
        suit_counts=dict(suit_counts),
        rank_counts={r: max(1, total // 13) for r in "23456789TJQKA"},
    )


def _full_deck() -> list:
    """Full 52-card deck as Card objects."""
    return [card(r, s) for s in "HDCS" for r in "23456789TJQKA"]


def _draw_pile_excluding(hand) -> list:
    """Draw pile = full 52 minus cards in hand (by rank+suit key).

    Real game deck_cards never includes cards currently in hand.
    """
    used = {(c.value.rank, c.value.suit) for c in hand if c.value.rank}
    return [c for c in _full_deck() if (c.value.rank, c.value.suit) not in used]


def _commit(**overrides):
    """Helper to call choose_mouth_commit with sensible defaults + seeded RNG."""
    random.seed(42)  # deterministic MC for test stability
    hand = overrides.get("hand_cards", [])
    default_deck = _draw_pile_excluding(hand)
    params = dict(
        hand_cards=[],
        hand_levels=_HAND_LEVELS,
        jokers=[],
        joker_limit=5,
        hands_left=4,
        discards_left=3,
        money=4,
        deck_cards=default_deck,
        deck_profile=_full_deck_profile(),
        ancient_suit=None,
        idol_rank=None,
        idol_suit=None,
        forced_card_idx=None,
        blind_name="The Mouth",
        ox_most_played=None,
    )
    params.update(overrides)
    return choose_mouth_commit(**params)


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


# ---------------------------------------------------------------------------
# choose_mouth_commit — end-to-end commitment decisions
# ---------------------------------------------------------------------------

class TestFormableCommits:
    def test_natural_flush_beats_other_types(self):
        """A natural Flush in hand dominates via raw score × repeatability."""
        hand = [card("A", "H"), card("K", "H"), card("Q", "H"), card("J", "H"),
                card("T", "H"), card("3", "C"), card("5", "S"), card("7", "D")]
        # Heart-heavy deck so flush repeats
        dp = _full_deck_profile(suit_counts={"H": 25, "D": 9, "C": 9, "S": 9})
        result = _commit(hand_cards=hand, deck_profile=dp)
        # Natural 5-card flush; Royal/Straight/Straight Flush classify as one of these.
        assert result in ("Flush", "Royal Flush", "Straight Flush")

    def test_commit_when_discards_zero_only_formable(self):
        """discards_left=0 disables chase; commit picks best formable."""
        hand = [card("K", "H"), card("K", "D"), card("3", "C"), card("5", "S"),
                card("7", "H"), card("9", "D"), card("2", "C"), card("4", "S")]
        result = _commit(hand_cards=hand, discards_left=0)
        # Only formable is Pair (and High Card). Pair scores higher.
        assert result == "Pair"


class TestChaseCommits:
    def test_flush_chase_wins_with_heart_rich_deck(self):
        """4 hearts in hand + heart-rich deck → Flush chase wins."""
        hand = [card("A", "H"), card("K", "H"), card("Q", "H"), card("J", "H"),
                card("3", "C"), card("5", "D"), card("7", "S"), card("9", "C")]
        # Heart-rich deck profile: many hearts remain
        dp = _full_deck_profile(suit_counts={"H": 20, "D": 10, "C": 10, "S": 10})
        # Custom draw pile: many hearts
        deck = [card(r, "H") for r in "23456789TJQKA" if r not in {"A","K","Q","J"}] * 2
        deck += [card(r, "D") for r in "23456789TJQKA" if r not in {"5"}]
        deck += [card(r, "C") for r in "23456789TJQKA" if r not in {"3","9"}]
        deck += [card(r, "S") for r in "23456789TJQKA" if r not in {"7"}]
        result = _commit(hand_cards=hand, deck_cards=deck, deck_profile=dp)
        # Straight Flush / Royal Flush also possible via a smaller straight window,
        # but the dominant chase is Flush (4-suited + 1 more heart).
        assert result in ("Flush", "Straight Flush", "Royal Flush")

    def test_chase_upgrade_from_pair_with_many_copies_in_deck(self):
        """Pair formable + extra copies of pair rank in deck → upgrade to Three of a Kind / Two Pair."""
        hand = [card("K", "H"), card("K", "D"), card("3", "C"), card("5", "S"),
                card("7", "H"), card("9", "D"), card("2", "C"), card("4", "S")]
        # Stock deck has 2 more Kings + plenty of other ranks → chase upgrades beat
        # committing to plain Pair.
        result = _commit(hand_cards=hand)
        # Accept any of the legitimate upgrade targets; explicitly reject "Pair"
        # which would mean the model failed to notice the upgrade path.
        assert result in ("Two Pair", "Three of a Kind", "Full House",
                          "Four of a Kind", "Straight", "Flush")


class TestForceChase:
    def test_empty_hand_returns_none(self):
        """No hand, nothing to commit to."""
        result = _commit(hand_cards=[])
        assert result is None

    def test_hands_left_zero_returns_none(self):
        """No hands to play → no commit needed."""
        hand = [card("K", "H"), card("K", "D"), card("3", "C"), card("5", "S"),
                card("7", "H"), card("9", "D"), card("2", "C"), card("4", "S")]
        result = _commit(hand_cards=hand, hands_left=0)
        assert result is None


class TestMouthCommitRegression:
    def test_returns_valid_type_or_none(self):
        """Basic sanity: always returns a valid hand type name or None."""
        hand = [card("7", "H"), card("3", "D"), card("9", "C"), card("Q", "S"),
                card("2", "H"), card("5", "D"), card("T", "C"), card("A", "S")]
        result = _commit(hand_cards=hand)
        assert result is None or result in _HAND_LEVELS

    def test_deterministic_with_seeded_rng(self):
        """Same hand + same seed → same commitment (MC variance controlled)."""
        hand = [card("A", "H"), card("K", "H"), card("Q", "H"), card("J", "H"),
                card("3", "C"), card("5", "D"), card("7", "S"), card("9", "C")]
        r1 = _commit(hand_cards=hand)
        r2 = _commit(hand_cards=hand)
        assert r1 == r2
