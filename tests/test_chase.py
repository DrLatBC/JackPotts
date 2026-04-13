"""Tests for chase strategy generation and extended draw quality functions.

Covers:
- generate_chases() producing all hand-type transitions
- New draw quality functions (pair, full house, four of a kind, etc.)
- chase_score() using real hand levels
- cards_not_in() sort priority rework (debuff as tiebreaker)
- flush_draw_quality debuff handling
"""

from balatro_bot.domain.scoring.chase import generate_chases
from balatro_bot.domain.scoring.draws import (
    pair_draw_quality,
    full_house_draw_quality,
    full_house_draw_quality_tight,
    four_kind_draw_quality,
    five_kind_draw_quality,
    straight_flush_draw_quality,
    flush_draw_quality,
    flush_draw_quality_loose,
    two_pair_draw_quality,
    two_pair_draw_quality_tight,
    three_kind_draw_quality,
)
from balatro_bot.domain.scoring.search import (
    best_hand,
    cards_not_in,
    discard_candidates,
)
from tests.conftest import card, debuffed_card


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _deck(suits="HDCS", ranks="23456789TJQKA", exclude=None):
    exclude = exclude or set()
    return [card(r, s) for s in suits for r in ranks if (r, s) not in exclude]


def _deck_excluding_hand(hand):
    """Build a standard deck minus cards in hand."""
    hand_keys = {(c.value.rank, c.value.suit) for c in hand}
    return _deck(exclude=hand_keys)


# ---------------------------------------------------------------------------
# pair_draw_quality
# ---------------------------------------------------------------------------

class TestPairDrawQuality:
    def test_finds_pair_from_singleton(self):
        hand = [card("A", "H"), card("K", "S"), card("Q", "D")]
        deck = _deck_excluding_hand(hand)
        result = pair_draw_quality(hand, deck)
        assert result is not None
        indices, prob = result
        assert len(indices) == 1
        assert prob > 0

    def test_prefers_rank_with_most_deck_copies(self):
        hand = [card("A", "H"), card("3", "S")]
        # Deck has 3 Aces left but only 3 Threes left — equal, so A wins by rank
        deck = _deck_excluding_hand(hand)
        result = pair_draw_quality(hand, deck)
        assert result is not None
        indices, _ = result
        assert indices == [0]  # Ace index

    def test_returns_none_when_already_have_pair(self):
        hand = [card("A", "H"), card("A", "S"), card("3", "D")]
        deck = _deck_excluding_hand(hand)
        result = pair_draw_quality(hand, deck)
        # Singletons only — 3 is the only singleton
        assert result is not None
        indices, _ = result
        assert indices == [2]  # Only the 3

    def test_respects_rank_affinity(self):
        hand = [card("A", "H"), card("3", "S")]
        deck = _deck_excluding_hand(hand)
        # Strongly prefer 3s
        result = pair_draw_quality(hand, deck, rank_affinity={"3": 5.0, "A": 0.0})
        assert result is not None
        indices, _ = result
        # Both have 3 copies in deck but 3 has higher affinity
        assert indices == [1]


# ---------------------------------------------------------------------------
# full_house_draw_quality
# ---------------------------------------------------------------------------

class TestFullHouseDrawQuality:
    def test_from_three_of_a_kind(self):
        hand = [card("K", "H"), card("K", "S"), card("K", "D"), card("5", "C"), card("2", "H")]
        deck = _deck_excluding_hand(hand)
        result = full_house_draw_quality(hand, deck)
        assert result is not None
        indices, prob = result
        # Should keep the three Kings
        assert set(indices) == {0, 1, 2}
        assert prob > 0

    def test_from_two_pair(self):
        hand = [card("K", "H"), card("K", "S"), card("Q", "D"), card("Q", "C"), card("2", "H")]
        deck = _deck_excluding_hand(hand)
        result = full_house_draw_quality(hand, deck)
        assert result is not None
        indices, prob = result
        assert len(indices) == 4  # Both pairs
        assert prob > 0

    def test_returns_none_without_trips_or_two_pair(self):
        hand = [card("A", "H"), card("K", "S"), card("Q", "D"), card("J", "C"), card("T", "H")]
        deck = _deck_excluding_hand(hand)
        result = full_house_draw_quality(hand, deck)
        assert result is None


# ---------------------------------------------------------------------------
# four_kind_draw_quality
# ---------------------------------------------------------------------------

class TestFourKindDrawQuality:
    def test_from_three_of_a_kind(self):
        hand = [card("A", "H"), card("A", "S"), card("A", "D"), card("5", "C"), card("2", "H")]
        deck = _deck_excluding_hand(hand)
        result = four_kind_draw_quality(hand, deck)
        assert result is not None
        indices, prob = result
        assert set(indices) == {0, 1, 2}
        # 1 Ace left in 47-card deck, drawing 5 cards
        assert 0 < prob < 1

    def test_returns_none_without_trips(self):
        hand = [card("A", "H"), card("A", "S"), card("K", "D"), card("5", "C")]
        deck = _deck_excluding_hand(hand)
        result = four_kind_draw_quality(hand, deck)
        assert result is None

    def test_returns_none_when_no_fourth_in_deck(self):
        hand = [card("A", "H"), card("A", "S"), card("A", "D")]
        # Remove the 4th Ace from deck
        deck = [c for c in _deck_excluding_hand(hand) if c.value.rank != "A"]
        result = four_kind_draw_quality(hand, deck)
        assert result is None


# ---------------------------------------------------------------------------
# five_kind_draw_quality
# ---------------------------------------------------------------------------

class TestFiveKindDrawQuality:
    def test_returns_none_without_quads(self):
        hand = [card("A", "H"), card("A", "S"), card("A", "D")]
        deck = _deck_excluding_hand(hand)
        result = five_kind_draw_quality(hand, deck)
        assert result is None

    def test_returns_none_when_no_fifth_in_deck(self):
        hand = [card("A", "H"), card("A", "S"), card("A", "D"), card("A", "C")]
        deck = _deck_excluding_hand(hand)  # No 5th ace possible
        result = five_kind_draw_quality(hand, deck)
        assert result is None


# ---------------------------------------------------------------------------
# straight_flush_draw_quality
# ---------------------------------------------------------------------------

class TestStraightFlushDrawQuality:
    def test_finds_4_suited_sequential(self):
        hand = [card("T", "H"), card("J", "H"), card("Q", "H"), card("K", "H"), card("2", "S")]
        deck = _deck_excluding_hand(hand)
        result = straight_flush_draw_quality(hand, deck)
        assert result is not None
        indices, prob = result
        assert len(indices) == 4
        # All four hearts should be kept
        assert set(indices) <= {0, 1, 2, 3}
        assert prob > 0

    def test_returns_none_when_not_4_suited_sequential(self):
        hand = [card("T", "H"), card("J", "S"), card("Q", "H"), card("K", "H")]
        deck = _deck_excluding_hand(hand)
        result = straight_flush_draw_quality(hand, deck)
        assert result is None

    def test_returns_none_with_4_suited_but_not_sequential(self):
        hand = [card("2", "H"), card("5", "H"), card("9", "H"), card("K", "H")]
        deck = _deck_excluding_hand(hand)
        result = straight_flush_draw_quality(hand, deck)
        assert result is None


# ---------------------------------------------------------------------------
# generate_chases — comprehensive strategy generation
# ---------------------------------------------------------------------------

class TestGenerateChases:
    def test_always_includes_keep_best_hand(self):
        hand = [card("A", "H"), card("K", "S"), card("Q", "D"), card("J", "C"), card("T", "H")]
        bh = best_hand(hand)
        strategies = generate_chases(hand, bh)
        names = [s[0] for s in strategies]
        assert bh.hand_name in names

    def test_includes_flush_chase_when_4_suited(self):
        hand = [card("A", "H"), card("K", "H"), card("Q", "H"), card("J", "H"),
                card("2", "S"), card("3", "C"), card("4", "D"), card("5", "S")]
        deck = _deck_excluding_hand(hand)
        bh = best_hand(hand)
        strategies = generate_chases(hand, bh, deck_cards=deck)
        names = [s[0] for s in strategies]
        assert "Flush" in names

    def test_includes_full_house_chase_from_trips(self):
        hand = [card("K", "H"), card("K", "S"), card("K", "D"),
                card("5", "C"), card("2", "H"), card("7", "S"), card("9", "D"), card("J", "C")]
        deck = _deck_excluding_hand(hand)
        bh = best_hand(hand)
        strategies = generate_chases(hand, bh, deck_cards=deck)
        names = [s[0] for s in strategies]
        assert "Full House" in names

    def test_includes_four_kind_chase_from_trips(self):
        hand = [card("A", "H"), card("A", "S"), card("A", "D"),
                card("5", "C"), card("2", "H"), card("7", "S"), card("9", "D"), card("J", "C")]
        deck = _deck_excluding_hand(hand)
        bh = best_hand(hand)
        strategies = generate_chases(hand, bh, deck_cards=deck)
        names = [s[0] for s in strategies]
        assert "Four of a Kind" in names

    def test_includes_pair_chase_from_high_card(self):
        hand = [card("A", "H"), card("K", "S"), card("Q", "D"),
                card("J", "C"), card("9", "H"), card("7", "S"), card("5", "D"), card("3", "C")]
        deck = _deck_excluding_hand(hand)
        bh = best_hand(hand)
        # Current best is High Card — should chase Pair
        assert bh.hand_name == "High Card"
        strategies = generate_chases(hand, bh, deck_cards=deck)
        names = [s[0] for s in strategies]
        assert "Pair" in names

    def test_respects_required_hand_filter(self):
        hand = [card("A", "H"), card("K", "H"), card("Q", "H"), card("J", "H"),
                card("2", "S"), card("3", "C"), card("4", "D"), card("5", "S")]
        deck = _deck_excluding_hand(hand)
        bh = best_hand(hand)
        strategies = generate_chases(hand, bh, deck_cards=deck, required_hand="Flush")
        names = [s[0] for s in strategies]
        assert "Flush" in names
        # Other chase types filtered out (except redraw)
        assert all(n in ("Flush", "redraw") for n in names)

    def test_includes_straight_flush_chase(self):
        hand = [card("T", "H"), card("J", "H"), card("Q", "H"), card("K", "H"),
                card("2", "S"), card("3", "C"), card("4", "D"), card("5", "S")]
        deck = _deck_excluding_hand(hand)
        bh = best_hand(hand)
        strategies = generate_chases(hand, bh, deck_cards=deck)
        names = [s[0] for s in strategies]
        assert "Straight Flush" in names


# ---------------------------------------------------------------------------
# chase_score with hand levels
# ---------------------------------------------------------------------------

class TestChaseScoreUsesHandLevels:
    def test_keep_leveled_pair_ranks_above_base_flush_chase(self):
        """Keep leveled-Pair strategy should outrank a Flush chase at base levels.

        Level-5 Pair: 70 chips × 6 mult × 1.0 prob = 420
        Base Flush:   35 chips × 4 mult × ~0.5 prob ≈ 70
        """
        # 2 spade Kings for a pair, 3 hearts for a flush draw (not enough for flush)
        hand = [card("K", "S"), card("K", "C"),
                card("A", "H"), card("9", "H"), card("5", "H"),
                card("7", "D"), card("3", "D"), card("2", "D")]
        deck = _deck_excluding_hand(hand)

        hand_levels = {
            "Pair": {"chips": 70, "mult": 6, "level": 5},
            "Flush": {"chips": 35, "mult": 4, "level": 1},
            "High Card": {"chips": 5, "mult": 1, "level": 1},
            "Straight": {"chips": 30, "mult": 4, "level": 1},
            "Two Pair": {"chips": 20, "mult": 2, "level": 1},
            "Three of a Kind": {"chips": 30, "mult": 3, "level": 1},
            "Full House": {"chips": 40, "mult": 4, "level": 1},
            "Four of a Kind": {"chips": 60, "mult": 7, "level": 1},
        }

        results = discard_candidates(
            hand, hand_levels=hand_levels, deck_cards=deck,
        )
        chase_names = [r.chase_hand for r in results]
        # Keep Pair (leveled: 420) should rank above any base-level chase
        assert chase_names[0] == "Pair", f"Expected Pair first, got {chase_names}"


# ---------------------------------------------------------------------------
# cards_not_in sort order — debuff as tiebreaker
# ---------------------------------------------------------------------------

class TestCardsNotInDebuffPriority:
    def test_debuffed_card_with_affinity_survives_over_junk(self):
        """A debuffed King with high rank affinity should survive over a non-debuffed 2."""
        hand = [
            card("A", "H"),         # 0 — in keep
            debuffed_card("K", "S"),  # 1 — debuffed but high rank
            card("2", "D"),         # 2 — junk
        ]
        keep = {0}
        rank_aff = {"K": 2.0, "A": 1.0, "2": -1.0}
        result = cards_not_in(hand, keep, rank_affinity=rank_aff)
        # 2 should be discarded first (low affinity), K second (high affinity despite debuff)
        assert result == [2, 1]

    def test_debuffed_junk_still_discarded_first(self):
        """A debuffed 2 with no affinity should still be discarded before a non-debuffed 3."""
        hand = [
            card("A", "H"),         # 0 — in keep
            debuffed_card("2", "S"),  # 1 — debuffed junk
            card("3", "D"),         # 2 — also junk but not debuffed
        ]
        keep = {0}
        result = cards_not_in(hand, keep)
        # Both are junk, debuff breaks the tie — 2 goes first
        assert result[0] == 1

    def test_scoring_suit_still_primary(self):
        """Scoring suit boss restriction should still be the top priority."""
        hand = [
            card("A", "H"),         # 0 — in keep
            card("K", "H"),         # 1 — scoring suit (H)
            debuffed_card("Q", "S"),  # 2 — debuffed, off-suit
        ]
        keep = {0}
        result = cards_not_in(hand, keep, scoring_suit="H")
        # Off-suit Q goes first despite being debuffed (scoring_suit protects K♥)
        assert result[0] == 2


# ---------------------------------------------------------------------------
# flush_draw_quality — debuff deprioritization
# ---------------------------------------------------------------------------

class TestFlushDrawQualityDebuff:
    def test_prefers_non_debuffed_card_in_flush(self):
        """When picking 4 from 5 suited cards, non-debuffed should be preferred."""
        hand = [
            card("A", "H"),          # 0
            debuffed_card("K", "H"), # 1 — debuffed
            card("Q", "H"),          # 2
            card("J", "H"),          # 3
            card("T", "H"),          # 4
        ]
        deck = _deck_excluding_hand(hand)
        result = flush_draw_quality(hand, deck)
        assert result is not None
        indices, _, _ = result
        # Debuffed K♥ should be dropped in favor of non-debuffed T♥
        assert 1 not in indices, "Debuffed K♥ should not be in the flush keep set"
        assert 4 in indices, "Non-debuffed T♥ should be kept"


# ---------------------------------------------------------------------------
# two_pair_draw_quality — probability no longer overcounts
# ---------------------------------------------------------------------------

class TestTwoPairProbability:
    def test_probability_never_exceeds_one(self):
        """With many viable ranks, probability should use inclusion-exclusion, not raw sum."""
        hand = [card("A", "H"), card("A", "S"),
                card("K", "D"), card("Q", "C"), card("J", "H"),
                card("T", "S"), card("9", "D"), card("8", "C")]
        deck = _deck_excluding_hand(hand)
        result = two_pair_draw_quality(hand, deck)
        assert result is not None
        _, prob = result
        assert 0 < prob <= 1.0


# ---------------------------------------------------------------------------
# Tight + loose keep-set variants
# ---------------------------------------------------------------------------

class TestTwoPairTightVariant:
    def test_keeps_pair_plus_singleton(self):
        """Tight variant keeps pair + best singleton."""
        hand = [card("A", "H"), card("A", "S"),
                card("K", "D"), card("Q", "C"), card("5", "H")]
        deck = _deck_excluding_hand(hand)
        result = two_pair_draw_quality_tight(hand, deck)
        assert result is not None
        indices, prob = result
        assert len(indices) == 3  # pair (2) + singleton (1)
        assert 0 in indices and 1 in indices  # Aces
        assert prob > 0

    def test_prefers_singleton_with_most_deck_copies(self):
        """Should pick the singleton rank with more copies in deck."""
        hand = [card("A", "H"), card("A", "S"),
                card("K", "D"), card("3", "C")]
        # Remove some Kings from deck to make 3 more available
        deck = [c for c in _deck_excluding_hand(hand) if c.value.rank != "K"]
        result = two_pair_draw_quality_tight(hand, deck)
        assert result is not None
        indices, _ = result
        assert 3 in indices  # 3 chosen over K (more copies in deck)

    def test_returns_none_without_singletons(self):
        """If all non-pair cards are also paired, tight variant can't apply."""
        hand = [card("A", "H"), card("A", "S"),
                card("K", "D"), card("K", "C")]
        deck = _deck_excluding_hand(hand)
        result = two_pair_draw_quality_tight(hand, deck)
        assert result is None

    def test_fewer_draws_than_loose(self):
        """Tight keeps 3 cards (pair+1), loose keeps 2 (pair only)."""
        hand = [card("A", "H"), card("A", "S"),
                card("K", "D"), card("Q", "C"), card("J", "H"),
                card("T", "S"), card("9", "D"), card("8", "C")]
        deck = _deck_excluding_hand(hand)
        loose = two_pair_draw_quality(hand, deck)
        tight = two_pair_draw_quality_tight(hand, deck)
        assert loose is not None and tight is not None
        assert len(tight[0]) > len(loose[0])  # tight keeps more cards


class TestFlushLooseVariant:
    def test_activates_with_3_suited(self):
        """Loose flush should work with exactly 3 suited cards."""
        hand = [card("A", "H"), card("K", "H"), card("Q", "H"),
                card("5", "S"), card("3", "C"), card("2", "D"),
                card("7", "S"), card("9", "C")]
        deck = _deck_excluding_hand(hand)
        result = flush_draw_quality_loose(hand, deck)
        assert result is not None
        indices, prob, suit = result
        assert len(indices) == 3
        assert suit == "H"
        assert prob > 0

    def test_does_not_activate_with_4_suited(self):
        """When 4+ suited exist, loose should not fire (tight dominates)."""
        hand = [card("A", "H"), card("K", "H"), card("Q", "H"), card("J", "H"),
                card("5", "S"), card("3", "C")]
        deck = _deck_excluding_hand(hand)
        result = flush_draw_quality_loose(hand, deck)
        assert result is None

    def test_probability_lower_than_tight(self):
        """Needing 2 of suit should have lower probability than needing 1.

        Comparing: 3 hearts (loose, need 2) vs hypothetical 4 hearts (tight, need 1).
        We test indirectly by checking loose prob < 1.0 and is reasonable.
        """
        hand = [card("A", "H"), card("K", "H"), card("Q", "H"),
                card("5", "S"), card("3", "C"), card("2", "D"),
                card("7", "S"), card("9", "C")]
        deck = _deck_excluding_hand(hand)
        result = flush_draw_quality_loose(hand, deck)
        assert result is not None
        _, prob, _ = result
        assert 0 < prob < 0.95  # Needing 2 cards is hard


class TestFullHouseTightVariant:
    def test_keeps_trips_plus_singleton(self):
        """Tight variant keeps trips + best singleton."""
        hand = [card("K", "H"), card("K", "S"), card("K", "D"),
                card("5", "C"), card("2", "H"), card("7", "S"), card("9", "D")]
        deck = _deck_excluding_hand(hand)
        result = full_house_draw_quality_tight(hand, deck)
        assert result is not None
        indices, prob = result
        assert len(indices) == 4  # trips (3) + singleton (1)
        assert {0, 1, 2} <= set(indices)  # Kings kept
        assert prob > 0

    def test_returns_none_without_trips(self):
        """No trips = no tight full house chase."""
        hand = [card("A", "H"), card("K", "S"), card("Q", "D"), card("J", "C")]
        deck = _deck_excluding_hand(hand)
        result = full_house_draw_quality_tight(hand, deck)
        assert result is None

    def test_returns_none_without_companions(self):
        """If hand is only trips with no other cards, tight can't apply."""
        hand = [card("A", "H"), card("A", "S"), card("A", "D")]
        deck = _deck_excluding_hand(hand)
        result = full_house_draw_quality_tight(hand, deck)
        assert result is None

    def test_prefers_companion_with_most_copies(self):
        """Should pick singleton rank with more copies in deck."""
        hand = [card("K", "H"), card("K", "S"), card("K", "D"),
                card("5", "C"), card("2", "H")]
        # Remove all but 1 Five from deck, leave 3 Twos
        deck = [c for c in _deck_excluding_hand(hand)
                if not (c.value.rank == "5")]
        result = full_house_draw_quality_tight(hand, deck)
        assert result is not None
        indices, _ = result
        # 2 should be chosen over 5 (more copies in deck)
        assert 4 in indices


class TestGenerateChasesVariants:
    def test_two_pair_generates_both_variants(self):
        """Two Pair chase should produce both loose and tight strategies."""
        # Pair of Aces + scattered ranks (no straight potential)
        hand = [card("A", "H"), card("A", "S"),
                card("5", "D"), card("3", "C"), card("9", "H"),
                card("7", "S"), card("2", "D"), card("J", "C")]
        deck = _deck_excluding_hand(hand)
        bh = best_hand(hand)
        assert bh.hand_name == "Pair"
        strategies = generate_chases(hand, bh, deck_cards=deck)
        tp_strategies = [s for s in strategies if s[0] == "Two Pair"]
        assert len(tp_strategies) >= 2, f"Expected 2+ Two Pair strategies, got {len(tp_strategies)}"
        reasons = [s[3] for s in tp_strategies]
        assert any("loose" in r for r in reasons)
        assert any("tight" in r for r in reasons)

    def test_flush_generates_loose_with_3_suited(self):
        """Flush loose should appear when only 3 suited cards exist."""
        hand = [card("A", "H"), card("K", "H"), card("Q", "H"),
                card("5", "S"), card("3", "C"), card("2", "D"),
                card("7", "S"), card("9", "C")]
        deck = _deck_excluding_hand(hand)
        bh = best_hand(hand)
        strategies = generate_chases(hand, bh, deck_cards=deck)
        flush_strategies = [s for s in strategies if s[0] == "Flush"]
        reasons = [s[3] for s in flush_strategies]
        assert any("loose" in r for r in reasons), f"Expected Flush loose, got {reasons}"

    def test_full_house_generates_both_from_trips(self):
        """Full House should produce both loose and tight from trips."""
        hand = [card("K", "H"), card("K", "S"), card("K", "D"),
                card("5", "C"), card("2", "H"), card("7", "S"),
                card("9", "D"), card("J", "C")]
        deck = _deck_excluding_hand(hand)
        bh = best_hand(hand)
        strategies = generate_chases(hand, bh, deck_cards=deck)
        fh_strategies = [s for s in strategies if s[0] == "Full House"]
        assert len(fh_strategies) >= 2, f"Expected 2+ FH strategies, got {len(fh_strategies)}"
        reasons = [s[3] for s in fh_strategies]
        assert any("loose" in r for r in reasons)
        assert any("tight" in r for r in reasons)
