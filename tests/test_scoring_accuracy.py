"""Tests for scoring accuracy fixes: The Arm, Flower Pot + WILD, Ride the Bus +
Pareidolia, Steel Joker, Bull Joker, and Glass card scoring."""

import math

from balatro_bot.hand_evaluator import score_hand, score_hand_detailed
from balatro_bot.cards import card_chip_value
from balatro_bot.context import arm_reduce_hand_levels, flint_halve_hand_levels
from tests.conftest import card, wild_card, stone_card, joker


def _joker_with_ability(key: str, ability: dict, **extra) -> dict:
    """Build a joker dict with a specific ability dict."""
    j = joker(key)
    j["value"] = {"ability": ability}
    j.update(extra)
    return j


# ---------------------------------------------------------------------------
# The Arm: hand level reduction
# ---------------------------------------------------------------------------

class TestArmHandLevelReduction:
    def test_reduces_pair_level_3_to_2(self):
        levels = {"Pair": {"chips": 40, "mult": 4, "level": 3}}
        reduced = arm_reduce_hand_levels(levels)
        assert reduced["Pair"]["chips"] == 25
        assert reduced["Pair"]["mult"] == 3
        assert reduced["Pair"]["level"] == 2

    def test_reduces_straight_level_4_to_3(self):
        levels = {"Straight": {"chips": 120, "mult": 13, "level": 4}}
        reduced = arm_reduce_hand_levels(levels)
        # Straight: +30 chips, +3 mult per level
        assert reduced["Straight"]["chips"] == 90
        assert reduced["Straight"]["mult"] == 10
        assert reduced["Straight"]["level"] == 3

    def test_does_not_go_below_level_1(self):
        levels = {"High Card": {"chips": 5, "mult": 1, "level": 1}}
        reduced = arm_reduce_hand_levels(levels)
        assert reduced["High Card"]["chips"] == 5
        assert reduced["High Card"]["mult"] == 1
        assert reduced["High Card"]["level"] == 1

    def test_reduces_flush_level_2_to_1(self):
        levels = {"Flush": {"chips": 50, "mult": 6, "level": 2}}
        reduced = arm_reduce_hand_levels(levels)
        # Flush base: 35/4, +15/+2 per level
        assert reduced["Flush"]["chips"] == 35
        assert reduced["Flush"]["mult"] == 4
        assert reduced["Flush"]["level"] == 1

    def test_reduces_full_house(self):
        levels = {"Full House": {"chips": 90, "mult": 8, "level": 3}}
        reduced = arm_reduce_hand_levels(levels)
        # Full House: +25/+2 per level
        assert reduced["Full House"]["chips"] == 65
        assert reduced["Full House"]["mult"] == 6

    def test_scoring_uses_reduced_level(self):
        """Score a Pair against The Arm — should use level-1 values."""
        # Pair level 4: 55 chips, 5 mult. Kings = 10+10 chips.
        levels = {"Pair": {"chips": 55, "mult": 5, "level": 4}}
        reduced = arm_reduce_hand_levels(levels)
        cards = [card("K", "H"), card("K", "D")]
        chips, mult, total = score_hand("Pair", cards, hand_levels=reduced)
        # Level 3: 40/4. Cards: 10+10=20. Total chips=60, mult=4, score=240
        assert chips == 60
        assert mult == 4
        assert total == 240

    def test_multiple_hand_types_all_reduced(self):
        levels = {
            "Pair": {"chips": 25, "mult": 3, "level": 2},
            "Flush": {"chips": 50, "mult": 6, "level": 2},
            "Straight": {"chips": 60, "mult": 7, "level": 2},
        }
        reduced = arm_reduce_hand_levels(levels)
        assert reduced["Pair"]["level"] == 1
        assert reduced["Flush"]["level"] == 1
        assert reduced["Straight"]["level"] == 1

    def test_preserves_non_dict_entries(self):
        levels = {"Pair": {"chips": 40, "mult": 4, "level": 3}, "garbage": 42}
        reduced = arm_reduce_hand_levels(levels)
        assert reduced["garbage"] == 42


# ---------------------------------------------------------------------------
# Flower Pot + WILD: test scoring with WILD cards
# ---------------------------------------------------------------------------

class TestFlowerPotWild:
    """Flower Pot requires all 4 suits among scoring cards.
    WILD cards count as all suits — verify our scorer handles this."""

    def _flower_pot_joker(self):
        return _joker_with_ability("j_flower_pot", {"extra": 3.0})

    def test_flower_pot_triggers_with_4_natural_suits(self):
        """All 4 natural suits present — Flower Pot should trigger."""
        cards = [card("A", "H"), card("K", "D"), card("Q", "C"), card("J", "S"), card("9", "H")]
        _, mult, _ = score_hand("High Card", [cards[0]], jokers=[self._flower_pot_joker()])
        # High Card with 1 scoring card A♥ — only 1 suit, Flower Pot shouldn't trigger
        assert mult == 1.0

        # Full hand with all 4 suits in scoring
        _, mult, _ = score_hand("Flush", cards, jokers=[self._flower_pot_joker()])
        # Not a flush (mixed suits), but if it were scored as High Card with all 5...
        # Let's use a hand type where all cards score
        _, mult, total = score_hand("Straight", cards, jokers=[self._flower_pot_joker()])
        # All 4 suits present among scoring cards — Flower Pot x3
        assert mult > 1.0  # Flower Pot should fire

    def test_flower_pot_does_not_trigger_with_3_suits(self):
        """Only 3 suits among scoring cards — Flower Pot should NOT trigger."""
        cards = [card("A", "H"), card("K", "D"), card("Q", "C"), card("J", "C"), card("9", "H")]
        # Straight: A-K-Q-J-9 is not a straight. Use T instead.
        cards_str = [card("A", "H"), card("K", "D"), card("Q", "C"), card("J", "C"), card("T", "H")]
        _, _, total_with = score_hand("Straight", cards_str, jokers=[self._flower_pot_joker()])
        _, _, total_without = score_hand("Straight", cards_str)
        # Suits: H, D, C, C, H — only 3 suits. Flower Pot should NOT trigger.
        assert total_with == total_without

    def test_flower_pot_with_wild_card_4th_suit(self):
        """WILD card provides the 4th suit — test that Flower Pot fires.
        This is our current behavior; if tests against the game show WILD
        doesn't count for Flower Pot, this test documents the expected fix."""
        cards = [card("A", "H"), card("K", "D"), card("Q", "C"), card("J", "C"), wild_card("T", "H")]
        # Natural suits: H, D, C, C + WILD (all suits) → 4 suits
        _, _, total_with = score_hand("Straight", cards, jokers=[self._flower_pot_joker()])
        _, _, total_without = score_hand("Straight", cards)
        # Currently: WILD makes card_suits return all 4, so Flower Pot triggers
        # Ratio should be 3x if Flower Pot fires
        if total_without > 0:
            ratio = total_with / total_without
            assert ratio > 2.5, f"Flower Pot should trigger with WILD providing 4th suit (ratio={ratio})"

    def test_flower_pot_all_same_suit_plus_wild(self):
        """All same natural suit + 1 WILD. Natural suits = 1, wild_count = 1, total = 2 < 4.
        WILD fills ONE missing suit, not all 3 — Flower Pot should NOT trigger."""
        cards = [card("A", "S"), card("K", "S"), card("Q", "S"), card("J", "S"), wild_card("T", "S")]
        _, _, total_with = score_hand("Straight", cards, jokers=[self._flower_pot_joker()])
        _, _, total_without = score_hand("Straight", cards)
        assert total_with == total_without, "Flower Pot should NOT trigger (only 2 suit coverage)"


# ---------------------------------------------------------------------------
# Ride the Bus + Pareidolia
# ---------------------------------------------------------------------------

class TestRideTheBusPareidolia:
    def test_ride_the_bus_resets_on_face_cards(self):
        """Ride the Bus should add 0 mult when face cards are scored."""
        cards = [card("K", "H"), card("K", "D")]
        rtb = _joker_with_ability("j_ride_the_bus", {"mult": 15, "extra": 1})
        _, mult, _ = score_hand("Pair", cards, jokers=[rtb])
        # K is a face card → resets, adds 0 mult
        assert mult == 2.0

    def test_ride_the_bus_adds_mult_without_faces(self):
        """Ride the Bus should add mult when no face cards scored."""
        cards = [card("9", "H"), card("9", "D")]
        rtb = _joker_with_ability("j_ride_the_bus", {"mult": 15, "extra": 1})
        _, mult, _ = score_hand("Pair", cards, jokers=[rtb])
        # No face cards → adds base(15) + extra(1) = 16
        assert mult == 18.0  # 2 (pair) + 16

    def test_ride_the_bus_resets_with_pareidolia(self):
        """With Pareidolia, ALL cards are face cards, so Ride the Bus always resets."""
        cards = [card("2", "H"), card("2", "D")]
        rtb = _joker_with_ability("j_ride_the_bus", {"mult": 15, "extra": 1})
        pareidolia = joker("j_pareidolia")
        _, mult, _ = score_hand("Pair", cards, jokers=[pareidolia, rtb])
        # Pareidolia makes 2s face cards → Ride the Bus resets, adds 0
        assert mult == 2.0

    def test_ride_the_bus_with_pareidolia_no_face_ranks(self):
        """Even with only numeric cards, Pareidolia makes them face → reset."""
        cards = [card("3", "C"), card("3", "S")]
        rtb = _joker_with_ability("j_ride_the_bus", {"mult": 20, "extra": 1})
        pareidolia = joker("j_pareidolia")
        _, mult_with, _ = score_hand("Pair", cards, jokers=[pareidolia, rtb])
        _, mult_without, _ = score_hand("Pair", cards, jokers=[rtb])
        # Without Pareidolia: 3s are not face → adds 20+1=21 mult → mult=23
        assert mult_without == 23.0
        # With Pareidolia: 3s become face → resets → mult=2
        assert mult_with == 2.0


# ---------------------------------------------------------------------------
# Steel Joker scoring
# ---------------------------------------------------------------------------

class TestSteelJokerScoring:
    def _steel_joker(self, xmult: float = 1.0):
        """Build Steel Joker with effect text showing the game's current xmult.

        The real API sends "Currently X1.6 Mult" etc. in the effect text.
        The bot now parses this directly instead of counting Steel cards.
        """
        effect = f"X{xmult} Mult" if xmult > 1.0 else ""
        j = _joker_with_ability("j_steel_joker", {"extra": 0.2})
        j["value"]["effect"] = effect
        return j

    def test_steel_joker_no_steel_cards(self):
        """Steel Joker with 0 Steel cards → xmult = 1.0 (no effect)."""
        cards = [card("K", "H"), card("K", "D")]
        _, _, total_with = score_hand("Pair", cards, jokers=[self._steel_joker(1.0)])
        _, _, total_without = score_hand("Pair", cards)
        assert total_with == total_without

    def test_steel_joker_with_steel_in_hand(self):
        """Steel Joker with 2 Steel cards → xmult = 1.4 (from parsed text)."""
        scoring = [card("K", "H"), card("K", "D")]
        held = [card("5", "S", enhancement="STEEL"), card("3", "H", enhancement="STEEL")]
        _, mult, _ = score_hand(
            "Pair", scoring, jokers=[self._steel_joker(xmult=1.4)],
            held_cards=held,
        )
        # Parsed xmult = 1.4 (game says "Currently X1.4 Mult")
        # Base mult: 2 (pair). Held Steel cards each add x1.5 mult.
        # Per-card Steel: mult = 2 * 1.5 * 1.5 = 4.5
        # Steel Joker: 4.5 * 1.4 = 6.3
        assert abs(mult - 6.3) < 0.01

    def test_steel_joker_with_steel_in_deck(self):
        """Steel Joker with 3 Steel cards in deck → xmult = 1.6."""
        scoring = [card("K", "H"), card("K", "D")]
        _, mult, _ = score_hand(
            "Pair", scoring, jokers=[self._steel_joker(xmult=1.6)],
        )
        # Parsed xmult = 1.6. Base mult: 2, no held Steel → mult = 2 * 1.6 = 3.2
        assert abs(mult - 3.2) < 0.01

    def test_steel_joker_counts_all_sources(self):
        """Steel Joker xmult comes from parsed text covering the full deck."""
        scoring = [card("K", "H", enhancement="STEEL"), card("K", "D")]
        held = [card("5", "S", enhancement="STEEL")]
        _, mult, _ = score_hand(
            "Pair", scoring, jokers=[self._steel_joker(xmult=1.6)],
            held_cards=held,
        )
        # Parsed xmult = 1.6 (game counted 3 Steel across full deck)
        # Held 5♠ Steel: x1.5 → mult = 2 * 1.5 = 3.0
        # Steel Joker: 3.0 * 1.6 = 4.8
        assert abs(mult - 4.8) < 0.01


# ---------------------------------------------------------------------------
# Bull Joker: money-dependent chips
# ---------------------------------------------------------------------------

class TestBullJokerMoney:
    def _bull_joker(self):
        return _joker_with_ability("j_bull", {"extra": 2})

    def test_bull_basic_scoring(self):
        """Bull adds +2 chips per dollar."""
        cards = [card("K", "H"), card("K", "D")]
        _, _, total = score_hand("Pair", cards, jokers=[self._bull_joker()], money=10)
        # Base: 30 chips, 2 mult. Bull: +2*10 = +20 chips. Total chips = 50. Score = 100
        assert total == 100

    def test_bull_zero_money(self):
        """Bull with $0 adds nothing."""
        cards = [card("K", "H"), card("K", "D")]
        _, _, total_with = score_hand("Pair", cards, jokers=[self._bull_joker()], money=0)
        _, _, total_without = score_hand("Pair", cards)
        assert total_with == total_without

    def test_bull_high_money(self):
        """Bull with $50 adds +100 chips."""
        cards = [card("K", "H"), card("K", "D")]
        _, _, total = score_hand("Pair", cards, jokers=[self._bull_joker()], money=50)
        # Base: 30 chips, 2 mult. Bull: +100. Total chips = 130. Score = 260
        assert total == 260

    def test_bull_money_changes_affect_score(self):
        """Different money amounts produce different scores."""
        cards = [card("K", "H"), card("K", "D")]
        bull = [self._bull_joker()]
        _, _, total_10 = score_hand("Pair", cards, jokers=bull, money=10)
        _, _, total_25 = score_hand("Pair", cards, jokers=bull, money=25)
        _, _, total_50 = score_hand("Pair", cards, jokers=bull, money=50)
        # Scores should increase with money
        assert total_10 < total_25 < total_50
        # Check exact values
        assert total_10 == 100   # 30+20=50 * 2
        assert total_25 == 160   # 30+50=80 * 2
        assert total_50 == 260   # 30+100=130 * 2


# ---------------------------------------------------------------------------
# Glass card scoring: always x2, not expected value
# ---------------------------------------------------------------------------

class TestGlassCardScoring:
    def test_glass_applies_x2(self):
        """Glass card should always apply x2 mult."""
        glass = card("K", "H", enhancement="GLASS")
        normal = card("K", "D")
        _, _, total_glass = score_hand("Pair", [glass, normal])
        _, _, total_normal = score_hand("Pair", [normal, card("K", "S")])
        # Glass K scores: chips added, then x2 mult on the glass card
        # Normal pair: 30 chips, 2 mult = 60
        # Glass pair: 30 chips, 2 * 2.0 = 4 mult = 120
        assert total_glass == 120
        assert total_normal == 60

    def test_glass_not_discounted(self):
        """Glass score should be full x2, not 0.75*x2 (expected value of 3/4 survive)."""
        glass = card("A", "H", enhancement="GLASS")
        _, mult, _ = score_hand("High Card", [glass])
        # High Card A: 5 base chips + 11 card chips = 16 chips
        # Glass x2 mult: 1 * 2.0 = 2.0
        assert mult == 2.0

    def test_multiple_glass_cards(self):
        """Multiple Glass cards each apply their x2 independently."""
        g1 = card("K", "H", enhancement="GLASS")
        g2 = card("K", "D", enhancement="GLASS")
        _, mult, total = score_hand("Pair", [g1, g2])
        # Both glass: mult = 2 * 2.0 * 2.0 = 8.0
        assert mult == 8.0
        assert total == 240  # 30 * 8


# ---------------------------------------------------------------------------
# Stone card perma_bonus (Hiker interaction)
# ---------------------------------------------------------------------------

class TestStoneCardPermaBonus:
    def test_stone_base_chips(self):
        """Stone card without perma_bonus gives 50 chips."""
        assert card_chip_value(stone_card()) == 50

    def test_stone_with_perma_bonus(self):
        """Stone card with perma_bonus (e.g. from Hiker) adds to 50 base."""
        sc = stone_card()
        sc["value"]["perma_bonus"] = 25
        assert card_chip_value(sc) == 75

    def test_stone_perma_bonus_zero(self):
        """Stone card with perma_bonus=0 still gives 50."""
        sc = stone_card()
        sc["value"]["perma_bonus"] = 0
        assert card_chip_value(sc) == 50


# ---------------------------------------------------------------------------
# Flower Pot with debuffed cards
# ---------------------------------------------------------------------------

class TestFlowerPotDebuffed:
    def _flower_pot_joker(self):
        return _joker_with_ability("j_flower_pot", {"extra": 3.0})

    def test_debuffed_card_provides_suit_for_flower_pot(self):
        """A debuffed card's suit still counts for Flower Pot's 4-suit check."""
        cards = [
            card("A", "H"),
            card("K", "D"),
            card("Q", "C"),
            card("J", "S"),
            card("T", "H"),
        ]
        # Debuff the Spade card — it should still provide its suit
        cards[3]["state"] = {"debuff": True}
        _, _, total_with = score_hand("Straight", cards, jokers=[self._flower_pot_joker()])
        _, _, total_without = score_hand("Straight", cards)
        # All 4 suits present (debuffed J♠ still contributes Spade)
        # Flower Pot should trigger (x3)
        assert total_with > total_without
        if total_without > 0:
            ratio = total_with / total_without
            assert ratio > 2.5, f"Flower Pot should trigger with debuffed 4th suit (ratio={ratio})"

    def test_debuffed_cards_not_enough_suits_without(self):
        """Without the debuffed card's suit, only 3 suits — Flower Pot shouldn't trigger."""
        cards = [
            card("A", "H"),
            card("K", "D"),
            card("Q", "C"),
            card("J", "C"),  # duplicate suit — only 3 natural suits
            card("T", "H"),
        ]
        _, _, total_with = score_hand("Straight", cards, jokers=[self._flower_pot_joker()])
        _, _, total_without = score_hand("Straight", cards)
        assert total_with == total_without, "Flower Pot should NOT trigger with only 3 suits"
