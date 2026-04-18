"""Tests for the unified joker valuation system."""

from __future__ import annotations

from tests.conftest import card, joker
from balatro_bot.domain.models.deck_profile import DeckProfile
from balatro_bot.domain.policy.shop_valuation import (
    _make_card,
    _synthetic_hand,
    _scoring_delta,
    _synergy_multiplier,
    _context_scale,
    _deck_composition_adjustment,
    evaluate_joker_value,
    UTILITY_VALUE,
)
from balatro_bot.domain.scoring.classify import classify_hand
from balatro_bot.strategy import compute_strategy, Strategy


# ---------------------------------------------------------------------------
# Synthetic hand builder tests
# ---------------------------------------------------------------------------

class TestSyntheticHand:
    def test_pair_produces_pair(self):
        scoring, played = _synthetic_hand("Pair")
        assert len(scoring) == 2
        assert len(played) == 5
        # Both scoring cards should have the same rank
        assert scoring[0].value.rank == scoring[1].value.rank

    def test_flush_produces_flush(self):
        scoring, played = _synthetic_hand("Flush")
        assert len(scoring) == 5
        suits = {c.value.suit for c in scoring}
        assert len(suits) == 1

    def test_straight_produces_straight(self):
        scoring, played = _synthetic_hand("Straight")
        assert len(scoring) == 5
        ranks = [c.value.rank for c in scoring]
        assert len(set(ranks)) == 5  # all different ranks

    def test_three_of_a_kind(self):
        scoring, played = _synthetic_hand("Three of a Kind")
        assert len(scoring) == 3
        assert len(played) == 5
        ranks = [c.value.rank for c in scoring]
        assert len(set(ranks)) == 1

    def test_full_house(self):
        scoring, played = _synthetic_hand("Full House")
        assert len(scoring) == 5
        assert len(played) == 5

    def test_four_of_a_kind(self):
        scoring, played = _synthetic_hand("Four of a Kind")
        assert len(scoring) == 4
        assert len(played) == 5
        ranks = [c.value.rank for c in scoring]
        assert len(set(ranks)) == 1

    def test_high_card(self):
        scoring, played = _synthetic_hand("High Card")
        assert len(scoring) == 1
        assert len(played) == 5

    def test_two_pair(self):
        scoring, played = _synthetic_hand("Two Pair")
        assert len(scoring) == 4
        assert len(played) == 5

    def test_strategy_preferred_suit(self):
        """Flush should use strategy's preferred suit."""
        strat = Strategy(
            preferred_hands=[("Flush", 5.0)],
            preferred_suits=[("S", 3.0)],
            preferred_ranks=[],
            active_archetypes=[],
        )
        scoring, played = _synthetic_hand("Flush", strategy=strat)
        suits = {c.value.suit for c in scoring}
        assert "S" in suits


# ---------------------------------------------------------------------------
# Scoring delta tests
# ---------------------------------------------------------------------------

class TestScoringDelta:
    def test_duo_with_pair_strategy(self):
        """Duo (X2 on Pair) should have high value when Pair is preferred."""
        duo = joker("j_duo", "Duo")
        duo["value"] = {"effect": "X2 Mult if played hand contains a Pair"}
        hand_levels = {"Pair": {"chips": 10, "mult": 10, "level": 1}}
        hand_types = [("Pair", 1.0)]

        delta = _scoring_delta(duo, [], hand_levels, hand_types)
        assert delta > 0.5  # substantial improvement from X2

    def test_duo_with_flush_strategy(self):
        """Duo should have near-zero value when Flush is the only preferred hand."""
        duo = joker("j_duo", "Duo")
        duo["value"] = {"effect": "X2 Mult if played hand contains a Pair"}
        hand_levels = {"Flush": {"chips": 35, "mult": 4, "level": 1}}
        hand_types = [("Flush", 1.0)]

        delta = _scoring_delta(duo, [], hand_levels, hand_types)
        assert delta < 0.1  # minimal improvement

    def test_unconditional_joker_always_contributes(self):
        """Joker (+4 mult) should contribute to any hand type."""
        j = joker("j_joker", "Joker")
        j["value"] = {"effect": "+4 Mult"}
        hand_levels = {"Pair": {"chips": 10, "mult": 10, "level": 1}}
        hand_types = [("Pair", 1.0)]

        delta = _scoring_delta(j, [], hand_levels, hand_types)
        assert delta > 0  # some improvement


# ---------------------------------------------------------------------------
# Synergy multiplier tests
# ---------------------------------------------------------------------------

class TestSynergyMultiplier:
    def test_pareidolia_boosts_photograph(self):
        """Pareidolia + Photograph should get amplification bonus."""
        owned = [joker("j_pareidolia")]
        owned_keys = {"j_pareidolia"}
        strat = Strategy([], [], [], [])

        mult = _synergy_multiplier("j_photograph", owned_keys, strat, owned)
        assert mult >= 2.5

    def test_no_synergy_baseline(self):
        """Without enablers, synergy should be near 1.0."""
        owned = [joker("j_joker")]
        owned_keys = {"j_joker"}
        strat = Strategy([], [], [], [])

        mult = _synergy_multiplier("j_photograph", owned_keys, strat, owned)
        assert mult == 1.0

    def test_coherence_with_shared_hand_type(self):
        """Two Pair-type jokers should get coherence bonus."""
        owned = [joker("j_jolly")]  # +mult on Pair
        owned_keys = {"j_jolly"}
        strat = Strategy([], [], [], [])

        mult = _synergy_multiplier("j_duo", owned_keys, strat, owned)
        assert mult > 1.0  # coherence bonus for sharing Pair

    def test_blueprint_xmult_synergy(self):
        """Blueprint should boost xMult joker value."""
        owned = [joker("j_blueprint")]
        owned_keys = {"j_blueprint"}
        strat = Strategy([], [], [], [])

        mult = _synergy_multiplier("j_duo", owned_keys, strat, owned)
        assert mult >= 1.3


# ---------------------------------------------------------------------------
# Context scaling tests
# ---------------------------------------------------------------------------

class TestContextScale:
    def test_xmult_boosted_late_game(self):
        """xMult jokers should be worth more at high antes."""
        early = _context_scale("j_duo", [], ante=2)
        late = _context_scale("j_duo", [], ante=7)
        assert late > early

    def test_flat_mult_dampened_late_game(self):
        """Flat mult jokers should be worth less at high antes."""
        early = _context_scale("j_jolly", [], ante=2)
        late = _context_scale("j_jolly", [], ante=7)
        assert late < early

    def test_diminishing_returns(self):
        """Third xMult joker should be worth less than the first."""
        no_owned = _context_scale("j_duo", [], ante=5)
        three_owned = _context_scale(
            "j_duo",
            [joker("j_tribe"), joker("j_trio"), joker("j_family")],
            ante=5,
        )
        assert three_owned < no_owned


# ---------------------------------------------------------------------------
# Full evaluate_joker_value tests
# ---------------------------------------------------------------------------

class TestEvaluateJokerValue:
    def test_utility_joker_has_value(self):
        """Utility jokers should get their base value."""
        j = joker("j_four_fingers")
        val = evaluate_joker_value(j, [], {}, ante=1)
        assert val > 0

    def test_scoring_joker_has_value(self):
        """A scoring joker with effect text should have positive value."""
        j = joker("j_joker", "Joker")
        j["value"] = {"effect": "+4 Mult"}
        hand_levels = {"Pair": {"chips": 10, "mult": 10, "level": 1}}
        val = evaluate_joker_value(j, [], hand_levels, ante=1)
        assert val > 0

    def test_xmult_joker_high_value(self):
        """xMult jokers should score high, especially late game."""
        duo = joker("j_duo", "Duo")
        duo["value"] = {"effect": "X2 Mult if played hand contains a Pair"}
        jolly = joker("j_jolly", "Jolly Joker")
        jolly["value"] = {"effect": "+8 Mult if played hand contains a Pair"}
        hand_levels = {"Pair": {"chips": 10, "mult": 10, "level": 1}}

        duo_val = evaluate_joker_value(duo, [], hand_levels, ante=5)
        jolly_val = evaluate_joker_value(jolly, [], hand_levels, ante=5)
        # Duo (X2) should be worth more than Jolly (+8 mult) at ante 5
        assert duo_val > jolly_val

    def test_scaling_joker_uses_accumulated_value(self):
        """A scaling joker with accumulated X3.0 should be very valuable."""
        madness = joker("j_madness", "Madness")
        madness["value"] = {
            "effect": "This Joker gains X0.5 Mult when blind is selected (Currently X3.0 Mult)"
        }
        val = evaluate_joker_value(madness, [], {}, ante=5)
        # X3.0 * 5.0 = 15.0 dynamic power, should be high
        assert val >= 10.0

    def test_unknown_joker_returns_zero(self):
        """An unknown joker with no effect should return 0."""
        j = joker("j_nonexistent_xyz")
        val = evaluate_joker_value(j, [], {}, ante=1)
        assert val == 0.0


# ---------------------------------------------------------------------------
# Baseball Card synergy tests
# ---------------------------------------------------------------------------

class TestBaseballCardSynergy:
    def test_baseball_synergy_scales_with_uncommon_count(self):
        """Baseball Card synergy multiplier should increase with Uncommon count."""
        strat = Strategy([], [], [], [])

        # 0 Uncommon jokers
        mult_0 = _synergy_multiplier(
            "j_baseball", set(), strat, [],
        )
        # 2 Uncommon jokers owned
        owned_2 = [
            joker("j_jolly", "Jolly Joker", rarity=2),
            joker("j_zany", "Zany Joker", rarity=2),
        ]
        mult_2 = _synergy_multiplier(
            "j_baseball", {"j_jolly", "j_zany"}, strat, owned_2,
        )
        # 3 Uncommon jokers owned
        owned_3 = owned_2 + [joker("j_crazy", "Crazy Joker", rarity=2)]
        mult_3 = _synergy_multiplier(
            "j_baseball", {"j_jolly", "j_zany", "j_crazy"}, strat, owned_3,
        )

        assert mult_0 == 1.0  # no Uncommons, no bonus
        assert mult_2 > mult_0  # 2 Uncommons boost Baseball
        assert mult_3 > mult_2  # 3 Uncommons boost even more

    def test_uncommon_candidate_boosted_when_baseball_owned(self):
        """Uncommon joker value should be boosted when Baseball Card is owned."""
        owned = [joker("j_baseball", "Baseball Card", rarity=2)]
        owned_keys = {"j_baseball"}
        strat = Strategy([], [], [], [])

        # Uncommon candidate with Baseball owned
        candidate = joker("j_jolly", "Jolly Joker", rarity=2)
        mult_with = _synergy_multiplier(
            "j_jolly", owned_keys, strat, owned, candidate=candidate,
        )
        # Same candidate without Baseball
        mult_without = _synergy_multiplier(
            "j_jolly", set(), strat, [], candidate=candidate,
        )

        assert mult_with > mult_without

    def test_common_candidate_not_boosted_by_baseball(self):
        """Common joker value should NOT be boosted by Baseball Card."""
        owned = [joker("j_baseball", "Baseball Card", rarity=2)]
        owned_keys = {"j_baseball"}
        strat = Strategy([], [], [], [])

        candidate = joker("j_joker", "Joker", rarity=1)
        mult = _synergy_multiplier(
            "j_joker", owned_keys, strat, owned, candidate=candidate,
        )

        assert mult == 1.0  # Common, no Baseball boost


# ---------------------------------------------------------------------------
# Deck composition adjustment tests
# ---------------------------------------------------------------------------


class TestDeckCompositionAdjustment:
    """Test _deck_composition_adjustment for enhancement-aware joker valuation."""

    def _profile_with_enhancements(self, **enh_counts):
        """Build a DeckProfile with specific enhancement counts."""
        cards = []
        for enh, count in enh_counts.items():
            for _ in range(count):
                cards.append(card("A", "H", enhancement=enh))
        # Pad with plain cards to make 52
        for _ in range(52 - len(cards)):
            cards.append(card("2", "S"))
        return DeckProfile.from_cards(cards)

    def test_steel_joker_no_steel_cards(self):
        dp = self._profile_with_enhancements()
        base = 5.0
        adjusted = _deck_composition_adjustment("j_steel_joker", base, dp, None)
        assert adjusted < base  # penalized

    def test_steel_joker_with_steel_cards(self):
        dp = self._profile_with_enhancements(STEEL=5)
        base = 5.0
        adjusted = _deck_composition_adjustment("j_steel_joker", base, dp, None)
        assert adjusted > base  # boosted

    def test_lucky_cat_with_lucky_cards(self):
        dp = self._profile_with_enhancements(LUCKY=4)
        base = 3.0
        adjusted = _deck_composition_adjustment("j_lucky_cat", base, dp, None)
        assert adjusted > base

    def test_glass_joker_no_glass(self):
        dp = self._profile_with_enhancements()
        base = 4.0
        adjusted = _deck_composition_adjustment("j_glass", base, dp, None)
        assert adjusted < base

    def test_drivers_license_below_threshold(self):
        dp = self._profile_with_enhancements(STEEL=5)
        base = 5.0
        adjusted = _deck_composition_adjustment("j_drivers_license", base, dp, None)
        assert adjusted == base * 0.1  # only 5 enhanced < 12

    def test_drivers_license_near_threshold(self):
        dp = self._profile_with_enhancements(STEEL=14)
        base = 5.0
        adjusted = _deck_composition_adjustment("j_drivers_license", base, dp, None)
        assert adjusted == base * 0.5  # 14 enhanced, between 12-16

    def test_drivers_license_above_threshold(self):
        dp = self._profile_with_enhancements(STEEL=16)
        base = 5.0
        adjusted = _deck_composition_adjustment("j_drivers_license", base, dp, None)
        assert adjusted == base  # 16 >= 16, full value

    def test_suit_joker_with_enhanced_suit(self):
        """Bloodstone (Hearts) should get a bonus when Hearts cards are enhanced."""
        cards = [card("A", "H", enhancement="STEEL") for _ in range(4)]
        cards += [card("2", "S") for _ in range(48)]
        dp = DeckProfile.from_cards(cards)
        base = 3.0
        adjusted = _deck_composition_adjustment("j_bloodstone", base, dp, None)
        assert adjusted > base  # Hearts suit has 4 enhanced cards

    def test_suit_joker_no_enhanced_suit(self):
        """Bloodstone gets no bonus when Hearts cards aren't enhanced."""
        cards = [card("A", "S", enhancement="STEEL") for _ in range(4)]  # Spades, not Hearts
        cards += [card("2", "H") for _ in range(48)]  # Hearts unenhanced
        dp = DeckProfile.from_cards(cards)
        base = 3.0
        adjusted = _deck_composition_adjustment("j_bloodstone", base, dp, None)
        assert adjusted == base

    def test_unrelated_joker_unaffected(self):
        dp = self._profile_with_enhancements(STEEL=10)
        base = 4.0
        adjusted = _deck_composition_adjustment("j_joker", base, dp, None)
        assert adjusted == base  # plain Joker not in any deck-comp list


class TestHeldCardJokerValuation:
    """Held-card jokers (Baron, Shoot the Moon, Raised Fist) need explicit
    deck-composition bonuses because _scoring_delta runs the synthetic hand
    with no held_cards and their effects never trigger in the simulation."""

    def _deck_with_ranks(self, **rank_counts):
        """Build a 52-card DeckProfile with specific rank counts."""
        cards = []
        for rank, count in rank_counts.items():
            for _ in range(count):
                cards.append(card(rank, "H"))
        for _ in range(52 - len(cards)):
            cards.append(card("2", "S"))
        return DeckProfile.from_cards(cards)

    def _deck_with_steel(self, steel_count):
        cards = [card("A", "H", enhancement="STEEL") for _ in range(steel_count)]
        for _ in range(52 - steel_count):
            cards.append(card("2", "S"))
        return DeckProfile.from_cards(cards)

    # --- Baron ---
    def test_baron_scales_with_king_count(self):
        dp4 = self._deck_with_ranks(K=4)
        dp8 = self._deck_with_ranks(K=8)
        adj4 = _deck_composition_adjustment("j_baron", 0.0, dp4, None)
        adj8 = _deck_composition_adjustment("j_baron", 0.0, dp8, None)
        assert adj4 > 0, "Baron with 4 Kings should have nonzero value"
        assert adj8 > adj4, "More Kings → more value"

    def test_baron_caps(self):
        dp20 = self._deck_with_ranks(K=20)
        adj = _deck_composition_adjustment("j_baron", 0.0, dp20, None)
        assert adj <= 8.0, "Baron bonus should cap at 8.0"

    # --- Shoot the Moon ---
    def test_shoot_the_moon_scales_with_queen_count(self):
        dp4 = self._deck_with_ranks(Q=4)
        dp8 = self._deck_with_ranks(Q=8)
        adj4 = _deck_composition_adjustment("j_shoot_the_moon", 0.0, dp4, None)
        adj8 = _deck_composition_adjustment("j_shoot_the_moon", 0.0, dp8, None)
        assert adj4 > 0
        assert adj8 > adj4

    # --- Raised Fist ---
    def test_raised_fist_early_ante_decent(self):
        dp = self._deck_with_steel(0)
        adj = _deck_composition_adjustment("j_raised_fist", 0.0, dp, None, ante=2)
        assert adj >= 1.5, f"RF at ante 2 no Steel should be decent, got {adj}"

    def test_raised_fist_mid_ante_weak(self):
        dp = self._deck_with_steel(0)
        adj_mid = _deck_composition_adjustment("j_raised_fist", 0.0, dp, None, ante=4)
        assert adj_mid == 0.0, f"RF at ante 4 no Steel should be 0, got {adj_mid}"

    def test_raised_fist_late_ante_bad_without_steel(self):
        dp = self._deck_with_steel(0)
        adj = _deck_composition_adjustment("j_raised_fist", 0.0, dp, None, ante=6)
        assert adj == 0.0

    def test_raised_fist_steel_overrides_ante_decay(self):
        """With Steel deck rolling, RF retains value even at late ante."""
        dp = self._deck_with_steel(6)
        adj_late = _deck_composition_adjustment("j_raised_fist", 0.0, dp, None, ante=6)
        assert adj_late >= 3.0, (
            f"RF + 6 Steel at ante 6 should be solid, got {adj_late}"
        )

    def test_raised_fist_monotonic_in_steel_count(self):
        adj_0 = _deck_composition_adjustment(
            "j_raised_fist", 0.0, self._deck_with_steel(0), None, ante=3,
        )
        adj_2 = _deck_composition_adjustment(
            "j_raised_fist", 0.0, self._deck_with_steel(2), None, ante=3,
        )
        adj_4 = _deck_composition_adjustment(
            "j_raised_fist", 0.0, self._deck_with_steel(4), None, ante=3,
        )
        assert adj_0 < adj_2 < adj_4


class TestEvaluateJokerWithDeckProfile:
    """Integration: evaluate_joker_value respects deck_profile."""

    def test_steel_joker_higher_with_steel_cards(self):
        candidate = joker("j_steel_joker", "Steel Joker")
        candidate.setdefault("value", {})["effect"] = "X1.0 Mult"
        owned = [joker("j_joker", "Joker")]
        hl = {"Pair": {"chips": 10, "mult": 2, "level": 1, "played": 0, "played_this_round": 0}}
        strat = compute_strategy(owned, hl)

        no_steel = DeckProfile.from_cards([card("A", "H") for _ in range(52)])
        with_steel = DeckProfile.from_cards(
            [card("A", "H", enhancement="STEEL") for _ in range(8)]
            + [card("2", "S") for _ in range(44)]
        )

        val_none = evaluate_joker_value(candidate, owned, hl, ante=3, strategy=strat,
                                         deck_profile=no_steel)
        val_steel = evaluate_joker_value(candidate, owned, hl, ante=3, strategy=strat,
                                          deck_profile=with_steel)
        assert val_steel > val_none
