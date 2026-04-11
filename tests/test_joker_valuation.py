"""Tests for the unified joker valuation system."""

from __future__ import annotations

from tests.conftest import card, joker
from balatro_bot.domain.policy.shop_valuation import (
    _make_card,
    _synthetic_hand,
    _scoring_delta,
    _synergy_multiplier,
    _context_scale,
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
