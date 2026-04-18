"""Tests for scoring accuracy fixes: The Arm, Flower Pot + WILD, Ride the Bus +
Pareidolia, Steel Joker, Bull Joker, and Glass card scoring."""

import math

from balatro_bot.domain.scoring.estimate import score_hand, score_hand_detailed
from balatro_bot.domain.scoring.base import arm_reduce_hand_levels, flint_halve_hand_levels
from balatro_bot.cards import card_chip_value
from dataclasses import replace as dc_replace
from tests.conftest import card, wild_card, stone_card, debuffed_card, joker


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
        sc = dc_replace(stone_card(), value=dc_replace(stone_card().value, perma_bonus=25))
        assert card_chip_value(sc) == 75

    def test_stone_perma_bonus_zero(self):
        """Stone card with perma_bonus=0 still gives 50."""
        sc = stone_card()
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
            debuffed_card("J", "S"),
            card("T", "H"),
        ]
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


# ---------------------------------------------------------------------------
# Vampire + Flower Pot: enhancement stripping propagation (issue #16)
# ---------------------------------------------------------------------------

class TestVampireFlowerPot:
    """Vampire strips enhancements in _apply_before_phase() which runs in the
    game's 'before' context (card.lua:3805). Flower Pot evaluates in the
    'joker_main' context (card.lua:4137). Since 'before' completes entirely
    before 'joker_main' starts, Vampire ALWAYS strips before Flower Pot
    evaluates — regardless of joker slot order. Regression test for issue #16."""

    def _vampire_joker(self, xmult: float = 1.0):
        return _joker_with_ability("j_vampire", {"extra": 0.1, "Xmult": xmult})

    def _flower_pot_joker(self):
        return _joker_with_ability("j_flower_pot", {"extra": 3.0})

    def _assert_flower_pot_does_not_fire(self, cards, jokers):
        """Flower Pot should NOT fire after Vampire strips the only WILD."""
        _, _, total_with_both = score_hand("Straight", cards, jokers=jokers)
        _, _, total_vampire_only = score_hand("Straight", cards, jokers=[self._vampire_joker()])
        if total_vampire_only > 0:
            ratio = total_with_both / total_vampire_only
            assert ratio < 2.0, (
                f"Flower Pot should NOT fire after Vampire strips WILD "
                f"(ratio={ratio:.2f}, expected ~1.0)"
            )

    def test_vampire_strips_wild_before_flower_pot(self):
        """Vampire strips WILD in the 'before' phase. Flower Pot then sees
        only 3 natural suits (H, D, C) in 'joker_main' and does NOT fire."""
        cards = [
            card("A", "H"),
            card("K", "D"),
            card("Q", "C"),
            card("J", "C"),
            wild_card("T", "H"),  # WILD provides 4th suit — but Vampire strips it
        ]
        # Vampire left of Flower Pot
        self._assert_flower_pot_does_not_fire(
            cards, [self._vampire_joker(), self._flower_pot_joker()])

    def test_vampire_right_of_flower_pot_still_strips(self):
        """Vampire fires in 'before' phase, Flower Pot in 'joker_main'.
        Even with Flower Pot LEFT of Vampire, Vampire still strips first."""
        cards = [
            card("A", "H"),
            card("K", "D"),
            card("Q", "C"),
            card("J", "C"),
            wild_card("T", "H"),
        ]
        # Flower Pot left of Vampire — should NOT matter
        self._assert_flower_pot_does_not_fire(
            cards, [self._flower_pot_joker(), self._vampire_joker()])

    def test_vampire_strips_wild_4_natural_suits_flower_pot_still_fires(self):
        """If all 4 natural suits are present even after WILD is stripped,
        Flower Pot should still fire."""
        cards = [
            card("A", "H"),
            card("K", "D"),
            card("Q", "C"),
            card("J", "S"),
            wild_card("T", "H"),  # WILD stripped, but 4 natural suits already present
        ]
        jokers = [self._vampire_joker(), self._flower_pot_joker()]
        _, _, total_with_both = score_hand("Straight", cards, jokers=jokers)
        _, _, total_vampire_only = score_hand("Straight", cards, jokers=[self._vampire_joker()])

        # 4 natural suits (H, D, C, S) present after stripping — Flower Pot fires
        if total_vampire_only > 0:
            ratio = total_with_both / total_vampire_only
            assert ratio > 2.5, (
                f"Flower Pot should fire with 4 natural suits post-strip "
                f"(ratio={ratio:.2f}, expected ~3.0)"
            )

    def test_no_vampire_wild_still_counts_for_flower_pot(self):
        """Without Vampire, WILD should still provide suit coverage for Flower Pot."""
        cards = [
            card("A", "H"),
            card("K", "D"),
            card("Q", "C"),
            card("J", "C"),
            wild_card("T", "H"),
        ]
        jokers = [self._flower_pot_joker()]
        _, _, total_with = score_hand("Straight", cards, jokers=jokers)
        _, _, total_without = score_hand("Straight", cards)

        if total_without > 0:
            ratio = total_with / total_without
            assert ratio > 2.5, (
                f"Without Vampire, WILD should let Flower Pot fire "
                f"(ratio={ratio:.2f}, expected ~3.0)"
            )

    def test_vampire_strips_mult_enhancement_flower_pot_unaffected(self):
        """Vampire strips a MULT enhancement (not WILD). Flower Pot should
        evaluate suits based on natural suits only — no change either way."""
        cards = [
            card("A", "H"),
            card("K", "D"),
            card("Q", "C"),
            card("J", "S"),
            card("T", "H", enhancement="MULT"),
        ]
        jokers = [self._vampire_joker(), self._flower_pot_joker()]
        _, _, total_both = score_hand("Straight", cards, jokers=jokers)
        _, _, total_fp_only = score_hand("Straight", cards, jokers=[self._flower_pot_joker()])

        # 4 natural suits present regardless of MULT stripping — Flower Pot fires in both
        assert total_both > 0
        assert total_fp_only > 0


# ---------------------------------------------------------------------------
# Midas Mask + Vampire: before-phase ordering (card.lua:3783, 3805)
# ---------------------------------------------------------------------------

class TestMidasVampireOrder:
    """Midas Mask and Vampire both fire in context.before (state_events.lua:637).
    Within that phase, jokers iterate left-to-right. Their relative order
    determines whether Midas-set GOLD gets stripped by Vampire or persists."""

    def _vampire_joker(self, xmult: float = 1.0):
        return _joker_with_ability("j_vampire", {"extra": 0.1, "Xmult": xmult})

    def _midas_joker(self):
        return _joker_with_ability("j_midas_mask", {})

    def test_midas_left_vampire_right_strips_gold(self):
        """Midas (left) sets face cards to GOLD, then Vampire (right) strips
        GOLD and gains xmult for those cards."""
        # K♥ is a face card — Midas will set it to GOLD, then Vampire strips it
        cards = [card("K", "H"), card("K", "D")]
        jokers = [self._midas_joker(), self._vampire_joker()]
        _, _, total = score_hand("Pair", cards, jokers=jokers)

        # Vampire should count the GOLD enhancement that Midas set
        # Baseline: no jokers
        _, _, base = score_hand("Pair", cards)
        # With Vampire stripping 2 GOLD enhancements: xmult = 1.0 + 0.1*2 = 1.2
        # Cards end up BASE (stripped), so no GOLD $3 bonus from card scoring
        assert total > base, "Vampire should apply xmult from Midas-set GOLD cards"

    def test_vampire_left_midas_right_keeps_gold(self):
        """Vampire (left) strips existing enhancements first, then Midas (right)
        sets face cards to GOLD. Face cards end up GOLD for card scoring."""
        # K♥ has MULT enhancement — Vampire strips it, then Midas sets GOLD
        cards = [card("K", "H", enhancement="MULT"), card("K", "D")]
        jokers = [self._vampire_joker(), self._midas_joker()]
        _, _, total_vm = score_hand("Pair", cards, jokers=jokers)

        # Same cards but Midas left of Vampire — Midas sets GOLD, Vampire strips it
        cards2 = [card("K", "H", enhancement="MULT"), card("K", "D")]
        jokers2 = [self._midas_joker(), self._vampire_joker()]
        _, _, total_mv = score_hand("Pair", cards2, jokers=jokers2)

        # Vampire-left: strips MULT (1 card), gains x1.1. Midas then sets GOLD.
        #   Cards score with GOLD enhancement during card scoring.
        # Midas-left: sets both to GOLD, Vampire strips both GOLD, gains x1.2.
        #   Cards score without enhancement.
        # Vampire-left should differ from Midas-left due to ordering
        assert total_vm != total_mv, (
            f"Order should matter: Vampire-left={total_vm} vs Midas-left={total_mv}"
        )

    def test_midas_alone_sets_gold_on_face_cards(self):
        """Midas alone converts face cards to GOLD enhancement."""
        cards = [card("K", "H"), card("Q", "D")]
        jokers = [self._midas_joker()]
        _, _, total_midas = score_hand("Pair", cards, jokers=jokers)

        # Without Midas, plain face cards
        _, _, total_base = score_hand("Pair", cards)
        # GOLD cards give $3 each when scored but no chip/mult bonus
        # The totals may differ due to GOLD replacing base enhancement
        assert total_midas > 0

    def test_midas_does_not_affect_non_face_cards(self):
        """Midas only converts face cards (J, Q, K). Number cards unaffected."""
        cards = [card("7", "H", enhancement="MULT"), card("7", "D")]
        jokers = [self._midas_joker()]
        _, _, total_midas = score_hand("Pair", cards, jokers=jokers)

        # Without Midas — should be the same since 7s aren't face cards
        _, _, total_base = score_hand("Pair", cards)
        assert total_midas == total_base, "Midas should not affect non-face cards"

    def test_midas_with_pareidolia_affects_all_cards(self):
        """With Pareidolia, all cards count as face cards for Midas.
        A MULT-enhanced number card should lose MULT and become GOLD."""
        # 7♥ has MULT (+4 mult). Pareidolia makes it a "face card" for Midas.
        # Midas converts it to GOLD, removing the MULT enhancement.
        cards = [card("7", "H", enhancement="MULT"), card("7", "D")]
        pareidolia = _joker_with_ability("j_pareidolia", {})
        jokers_with = [pareidolia, self._midas_joker()]
        _, mult_with, _ = score_hand("Pair", cards, jokers=jokers_with)

        jokers_without = [pareidolia]
        _, mult_without, _ = score_hand("Pair", cards, jokers=jokers_without)
        # Without Midas, 7♥ has MULT enhancement adding +4 mult
        # With Midas, 7♥ becomes GOLD (no +4 mult)
        assert mult_with < mult_without, (
            f"Pareidolia + Midas should strip MULT enhancement from number card "
            f"(mult_with={mult_with}, mult_without={mult_without})"
        )


# ---------------------------------------------------------------------------
# Raised Fist with debuffed held cards
# ---------------------------------------------------------------------------

class TestRaisedFistDebuffed:
    """Raised Fist picks the lowest-ranked held card INCLUDING debuffed ones.
    If that card is debuffed, the effect returns 0 mult (matching game logic)."""

    _HL = {"Pair": {"chips": 10, "mult": 2, "level": 1}}

    def _raised_fist_joker(self):
        return _joker_with_ability("j_raised_fist", {"x_mult": 1})

    def test_no_debuff_adds_mult(self):
        """Lowest held card (4) not debuffed → adds 2×4 = 8 mult."""
        played = [card("5", "H"), card("5", "D")]
        held = [card("A", "D"), card("Q", "D"), card("4", "C")]
        result = score_hand_detailed(
            "Pair", played, hand_levels=self._HL,
            jokers=[self._raised_fist_joker()],
            played_cards=played, held_cards=held,
        )
        assert result["pre_joker_mult"] == 10.0  # 2 base + 2*4

    def test_lowest_debuffed_adds_zero(self):
        """Lowest held card (4♣) debuffed by boss → 0 mult from Raised Fist."""
        played = [card("5", "H"), card("5", "D")]
        held = [card("A", "D"), card("Q", "D"), debuffed_card("4", "C")]
        result = score_hand_detailed(
            "Pair", played, hand_levels=self._HL,
            jokers=[self._raised_fist_joker()],
            played_cards=played, held_cards=held,
            blind_name="The Club",
        )
        assert result["pre_joker_mult"] == 2.0  # base only

    def test_non_lowest_debuffed_still_adds(self):
        """Higher card (K♣) debuffed, lowest (4♥) fine → adds 2×4 = 8."""
        played = [card("5", "H"), card("5", "D")]
        held = [debuffed_card("K", "C"), card("Q", "D"), card("4", "H")]
        result = score_hand_detailed(
            "Pair", played, hand_levels=self._HL,
            jokers=[self._raised_fist_joker()],
            played_cards=played, held_cards=held,
            blind_name="The Club",
        )
        assert result["pre_joker_mult"] == 10.0  # 2 base + 2*4

    def test_all_held_debuffed_adds_zero(self):
        """All held cards debuffed → 0 mult from Raised Fist."""
        played = [card("5", "H"), card("5", "D")]
        held = [debuffed_card("A", "C"), debuffed_card("Q", "C"), debuffed_card("4", "C")]
        result = score_hand_detailed(
            "Pair", played, hand_levels=self._HL,
            jokers=[self._raised_fist_joker()],
            played_cards=played, held_cards=held,
            blind_name="The Club",
        )
        assert result["pre_joker_mult"] == 2.0  # base only


# ---------------------------------------------------------------------------
# Blueprint / Brainstorm + held-card-phase jokers
# ---------------------------------------------------------------------------

class TestBlueprintHeldCardPhase:
    """Blueprint/Brainstorm should copy held-card-phase jokers (Baron,
    Shoot the Moon, Mime, Raised Fist) just like they copy per-card jokers."""

    _HL = {"Pair": {"chips": 10, "mult": 2, "level": 1}}

    def test_blueprint_copies_baron(self):
        """Blueprint to the left of Baron → x1.5 applied twice per held King.
        Held: K♣ (one King). Base mult 2, then 2 * 1.5 * 1.5 = 4.5."""
        played = [card("5", "H"), card("5", "D")]
        held = [card("K", "C"), card("Q", "D")]
        baron = _joker_with_ability("j_baron", {"extra": 1.5})
        blueprint = joker("j_blueprint")
        # Blueprint at index 0, Baron at index 1 (to its right)
        result = score_hand_detailed(
            "Pair", played, hand_levels=self._HL,
            jokers=[blueprint, baron],
            played_cards=played, held_cards=held,
        )
        assert result["pre_joker_mult"] == 2.0 * 1.5 * 1.5  # 4.5

    def test_brainstorm_copies_baron(self):
        """Brainstorm copies leftmost joker (Baron). Same double-x1.5 effect."""
        played = [card("5", "H"), card("5", "D")]
        held = [card("K", "C"), card("Q", "D")]
        baron = _joker_with_ability("j_baron", {"extra": 1.5})
        brainstorm = joker("j_brainstorm")
        # Baron at index 0 (leftmost), Brainstorm at index 1
        result = score_hand_detailed(
            "Pair", played, hand_levels=self._HL,
            jokers=[baron, brainstorm],
            played_cards=played, held_cards=held,
        )
        assert result["pre_joker_mult"] == 2.0 * 1.5 * 1.5  # 4.5

    def test_blueprint_copies_shoot_the_moon(self):
        """Blueprint copies Shoot the Moon → +13 applied twice per held Queen.
        Held: Q♣. Base mult 2, then 2 + 13 + 13 = 28."""
        played = [card("5", "H"), card("5", "D")]
        held = [card("Q", "C"), card("K", "D")]
        stm = _joker_with_ability("j_shoot_the_moon", {"extra": 13})
        blueprint = joker("j_blueprint")
        result = score_hand_detailed(
            "Pair", played, hand_levels=self._HL,
            jokers=[blueprint, stm],
            played_cards=played, held_cards=held,
        )
        assert result["pre_joker_mult"] == 2.0 + 13 + 13  # 28.0

    def test_blueprint_copies_mime(self):
        """Blueprint copies Mime → held cards trigger 3x (1 base + 1 Mime + 1 Blueprint-as-Mime).
        Held: K♣ with Baron present. Base mult 2, then 2 * 1.5^3 = 6.75."""
        played = [card("5", "H"), card("5", "D")]
        held = [card("K", "C")]
        baron = _joker_with_ability("j_baron", {"extra": 1.5})
        mime = joker("j_mime")
        blueprint = joker("j_blueprint")
        # Order: baron, blueprint, mime — Blueprint copies Mime (to its right)
        result = score_hand_detailed(
            "Pair", played, hand_levels=self._HL,
            jokers=[baron, blueprint, mime],
            played_cards=played, held_cards=held,
        )
        # 3 triggers of Baron x1.5 each: 2 * 1.5^3 = 6.75
        assert result["pre_joker_mult"] == 2.0 * 1.5 ** 3  # 6.75

    def test_debuffed_blueprint_no_copy(self):
        """Debuffed Blueprint should not copy Baron."""
        played = [card("5", "H"), card("5", "D")]
        held = [card("K", "C")]
        baron = _joker_with_ability("j_baron", {"extra": 1.5})
        blueprint = joker("j_blueprint")
        blueprint["state"] = {"debuff": True}
        result = score_hand_detailed(
            "Pair", played, hand_levels=self._HL,
            jokers=[blueprint, baron],
            played_cards=played, held_cards=held,
        )
        # Only Baron applies once: 2 * 1.5 = 3.0
        assert result["pre_joker_mult"] == 2.0 * 1.5  # 3.0

    def test_blueprint_debuffed_target_no_copy(self):
        """Blueprint targeting a debuffed Baron should not copy it."""
        played = [card("5", "H"), card("5", "D")]
        held = [card("K", "C")]
        baron = _joker_with_ability("j_baron", {"extra": 1.5})
        baron["state"] = {"debuff": True}
        blueprint = joker("j_blueprint")
        result = score_hand_detailed(
            "Pair", played, hand_levels=self._HL,
            jokers=[blueprint, baron],
            played_cards=played, held_cards=held,
        )
        # Neither applies: mult stays at base 2
        assert result["pre_joker_mult"] == 2.0
