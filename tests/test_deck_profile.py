"""Tests for DeckProfile — deck composition tracking."""

from tests.conftest import card, stone_card, wild_card
from balatro_bot.domain.models.deck_profile import DeckProfile


RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "T", "J", "Q", "K"]
SUITS = ["H", "D", "C", "S"]


def _standard_52() -> list:
    """Build a standard 52-card deck."""
    return [card(r, s) for s in SUITS for r in RANKS]


class TestBasicCounts:
    def test_standard_deck_total(self):
        dp = DeckProfile.from_cards(_standard_52())
        assert dp.total_cards == 52

    def test_standard_deck_suit_counts(self):
        dp = DeckProfile.from_cards(_standard_52())
        for s in SUITS:
            assert dp.suit_counts[s] == 13

    def test_standard_deck_rank_counts(self):
        dp = DeckProfile.from_cards(_standard_52())
        for r in RANKS:
            assert dp.rank_counts[r] == 4

    def test_no_enhancements_in_standard_deck(self):
        dp = DeckProfile.from_cards(_standard_52())
        assert dp.enhanced_card_count == 0
        assert dp.enhancement_counts == {}

    def test_empty_deck(self):
        dp = DeckProfile.from_cards([])
        assert dp.total_cards == 0
        assert dp.suit_counts == {}
        assert dp.rank_counts == {}


class TestEnhancements:
    def test_enhancement_counts(self):
        cards = [
            card("A", "H", enhancement="STEEL"),
            card("K", "H", enhancement="STEEL"),
            card("Q", "D", enhancement="LUCKY"),
            card("J", "C"),  # no enhancement
        ]
        dp = DeckProfile.from_cards(cards)
        assert dp.enhancement_counts["STEEL"] == 2
        assert dp.enhancement_counts["LUCKY"] == 1
        assert dp.enhanced_card_count == 3

    def test_enhancements_by_suit(self):
        cards = [
            card("A", "H", enhancement="STEEL"),
            card("K", "H", enhancement="STEEL"),
            card("Q", "D", enhancement="STEEL"),
            card("J", "C", enhancement="LUCKY"),
        ]
        dp = DeckProfile.from_cards(cards)
        assert dp.enhancements_by_suit["H"]["STEEL"] == 2
        assert dp.enhancements_by_suit["D"]["STEEL"] == 1
        assert dp.enhancements_by_suit["C"]["LUCKY"] == 1
        assert "S" not in dp.enhancements_by_suit

    def test_enhancements_by_rank(self):
        cards = [
            card("K", "H", enhancement="STEEL"),
            card("K", "D", enhancement="STEEL"),
            card("Q", "H", enhancement="LUCKY"),
        ]
        dp = DeckProfile.from_cards(cards)
        assert dp.enhancements_by_rank["K"]["STEEL"] == 2
        assert dp.enhancements_by_rank["Q"]["LUCKY"] == 1

    def test_base_enhancement_not_counted(self):
        from balatro_bot.domain.models.card import Card, CardModifier, CardValue, CardState
        c = Card(
            value=CardValue(rank="A", suit="H"),
            modifier=CardModifier(enhancement="BASE"),
            state=CardState(),
        )
        dp = DeckProfile.from_cards([c])
        assert dp.enhanced_card_count == 0


class TestWildCards:
    def test_wild_counts_in_all_suits(self):
        cards = [wild_card("A", "H")]
        dp = DeckProfile.from_cards(cards)
        for s in SUITS:
            assert dp.suit_counts.get(s, 0) == 1
        assert dp.total_cards == 1

    def test_wild_enhancement_tracked(self):
        cards = [wild_card("A", "H")]
        dp = DeckProfile.from_cards(cards)
        assert dp.enhancement_counts.get("WILD", 0) == 1
        assert dp.enhanced_card_count == 1

    def test_wild_suit_cross_reference_uses_base_suit(self):
        """Wild card's enhancement is attributed to its base suit, not all suits."""
        cards = [wild_card("A", "H")]
        dp = DeckProfile.from_cards(cards)
        assert dp.enhancements_by_suit["H"]["WILD"] == 1
        # Not attributed to other suits
        assert "D" not in dp.enhancements_by_suit


class TestStoneCards:
    def test_stone_excluded_from_ranks(self):
        cards = [stone_card(), card("A", "H")]
        dp = DeckProfile.from_cards(cards)
        assert dp.total_cards == 2
        assert dp.rank_counts.get("A", 0) == 1
        # Stone has no rank, shouldn't appear in rank_counts
        assert sum(dp.rank_counts.values()) == 1

    def test_stone_counted_as_enhanced(self):
        dp = DeckProfile.from_cards([stone_card()])
        assert dp.enhanced_card_count == 1
        assert dp.enhancement_counts.get("STONE", 0) == 1


class TestDriversLicense:
    def test_below_threshold(self):
        cards = [card("A", "H", enhancement="STEEL") for _ in range(15)]
        dp = DeckProfile.from_cards(cards)
        assert not dp.has_drivers_license_threshold

    def test_at_threshold(self):
        cards = [card("A", "H", enhancement="STEEL") for _ in range(16)]
        dp = DeckProfile.from_cards(cards)
        assert dp.has_drivers_license_threshold

    def test_above_threshold(self):
        cards = [card("A", "H", enhancement="STEEL") for _ in range(20)]
        dp = DeckProfile.from_cards(cards)
        assert dp.has_drivers_license_threshold


class TestConvenienceProperties:
    def test_dominant_suit(self):
        cards = [card("A", "H")] * 5 + [card("A", "D")] * 3
        dp = DeckProfile.from_cards(cards)
        assert dp.dominant_suit == "H"

    def test_dominant_suit_empty(self):
        dp = DeckProfile.from_cards([])
        assert dp.dominant_suit is None

    def test_enhancement_suit_concentration(self):
        cards = [
            card("A", "H", enhancement="STEEL"),
            card("K", "H", enhancement="STEEL"),
            card("Q", "D", enhancement="STEEL"),
        ]
        dp = DeckProfile.from_cards(cards)
        assert dp.enhancement_suit_concentration("STEEL") == "H"

    def test_enhancement_suit_concentration_none(self):
        dp = DeckProfile.from_cards([card("A", "H")])
        assert dp.enhancement_suit_concentration("STEEL") is None

    def test_enhancement_rank_concentration(self):
        cards = [
            card("K", "H", enhancement="LUCKY"),
            card("K", "D", enhancement="LUCKY"),
            card("Q", "H", enhancement="LUCKY"),
        ]
        dp = DeckProfile.from_cards(cards)
        assert dp.enhancement_rank_concentration("LUCKY") == "K"


class TestRawDictCards:
    """DeckProfile should also handle raw dict cards (not just Card objects)."""

    def test_dict_card(self):
        raw = {
            "value": {"rank": "A", "suit": "H"},
            "modifier": {"enhancement": "STEEL"},
        }
        dp = DeckProfile.from_cards([raw])
        assert dp.total_cards == 1
        assert dp.suit_counts["H"] == 1
        assert dp.rank_counts["A"] == 1
        assert dp.enhancement_counts["STEEL"] == 1
        assert dp.enhancements_by_suit["H"]["STEEL"] == 1
        assert dp.enhancements_by_rank["A"]["STEEL"] == 1
