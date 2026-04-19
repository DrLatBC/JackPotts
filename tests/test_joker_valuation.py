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
from balatro_bot.domain.policy.sim_context import SimContext
from balatro_bot.domain.scoring.classify import classify_hand
from balatro_bot.strategy import compute_strategy, Strategy


def _ctx(
    candidate: dict | None = None,
    owned: list[dict] | None = None,
    hand_levels: dict | None = None,
    strategy: Strategy | None = None,
    ante: int = 1,
) -> SimContext:
    """Tiny factory for tests that exercise the internal helpers directly."""
    cand = candidate if candidate is not None else {"key": "j_joker", "label": "Joker"}
    owned = owned if owned is not None else []
    hand_levels = hand_levels if hand_levels is not None else {
        "Pair": {"chips": 10, "mult": 2, "level": 1},
        "High Card": {"chips": 5, "mult": 1, "level": 1},
        "Flush": {"chips": 35, "mult": 4, "level": 1},
    }
    if strategy is None:
        strategy = compute_strategy(owned, hand_levels)
    return SimContext.build(
        candidate=cand,
        owned_jokers=owned,
        hand_levels=hand_levels,
        strategy=strategy,
        ante=ante,
    )


# ---------------------------------------------------------------------------
# Synthetic hand builder tests
# ---------------------------------------------------------------------------

class TestSyntheticHand:
    def test_pair_produces_pair(self):
        scoring, played = _synthetic_hand(_ctx(), "Pair")
        assert len(scoring) == 2
        assert len(played) == 5
        # Both scoring cards should have the same rank
        assert scoring[0].value.rank == scoring[1].value.rank

    def test_flush_produces_flush(self):
        scoring, played = _synthetic_hand(_ctx(), "Flush")
        assert len(scoring) == 5
        suits = {c.value.suit for c in scoring}
        assert len(suits) == 1

    def test_straight_produces_straight(self):
        scoring, played = _synthetic_hand(_ctx(), "Straight")
        assert len(scoring) == 5
        ranks = [c.value.rank for c in scoring]
        assert len(set(ranks)) == 5  # all different ranks

    def test_three_of_a_kind(self):
        scoring, played = _synthetic_hand(_ctx(), "Three of a Kind")
        assert len(scoring) == 3
        assert len(played) == 5
        ranks = [c.value.rank for c in scoring]
        assert len(set(ranks)) == 1

    def test_full_house(self):
        scoring, played = _synthetic_hand(_ctx(), "Full House")
        assert len(scoring) == 5
        assert len(played) == 5

    def test_four_of_a_kind(self):
        scoring, played = _synthetic_hand(_ctx(), "Four of a Kind")
        assert len(scoring) == 4
        assert len(played) == 5
        ranks = [c.value.rank for c in scoring]
        assert len(set(ranks)) == 1

    def test_high_card(self):
        scoring, played = _synthetic_hand(_ctx(), "High Card")
        assert len(scoring) == 1
        assert len(played) == 5

    def test_two_pair(self):
        scoring, played = _synthetic_hand(_ctx(), "Two Pair")
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
        scoring, played = _synthetic_hand(_ctx(strategy=strat), "Flush")
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

        delta = _scoring_delta(_ctx(candidate=duo, hand_levels=hand_levels), hand_types)
        assert delta > 0.5  # substantial improvement from X2

    def test_duo_with_flush_strategy(self):
        """Duo should have near-zero value when Flush is the only preferred hand."""
        duo = joker("j_duo", "Duo")
        duo["value"] = {"effect": "X2 Mult if played hand contains a Pair"}
        hand_levels = {"Flush": {"chips": 35, "mult": 4, "level": 1}}
        hand_types = [("Flush", 1.0)]

        delta = _scoring_delta(_ctx(candidate=duo, hand_levels=hand_levels), hand_types)
        assert delta < 0.1  # minimal improvement

    def test_unconditional_joker_always_contributes(self):
        """Joker (+4 mult) should contribute to any hand type."""
        j = joker("j_joker", "Joker")
        j["value"] = {"effect": "+4 Mult"}
        hand_levels = {"Pair": {"chips": 10, "mult": 10, "level": 1}}
        hand_types = [("Pair", 1.0)]

        delta = _scoring_delta(_ctx(candidate=j, hand_levels=hand_levels), hand_types)
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
    """What's left of ``_deck_composition_adjustment`` after Phase 3: only
    Drivers License (activation gate) and Blackboard (held-phase proc rate
    from deck-wide S/C density). Steel / Lucky / Glass / suit-enhancement
    boosts moved into the sim itself — see ``TestPhase3DensityAwareSim``."""

    def _profile_with_enhancements(self, **enh_counts):
        cards = []
        for enh, count in enh_counts.items():
            for _ in range(count):
                cards.append(card("A", "H", enhancement=enh))
        for _ in range(52 - len(cards)):
            cards.append(card("2", "S"))
        return DeckProfile.from_cards(cards)

    def test_drivers_license_below_threshold(self):
        dp = self._profile_with_enhancements(STEEL=5)
        base = 5.0
        adjusted = _deck_composition_adjustment("j_drivers_license", base, dp, None)
        assert adjusted == base * 0.1

    def test_drivers_license_near_threshold(self):
        dp = self._profile_with_enhancements(STEEL=14)
        base = 5.0
        adjusted = _deck_composition_adjustment("j_drivers_license", base, dp, None)
        assert adjusted == base * 0.5

    def test_drivers_license_above_threshold(self):
        dp = self._profile_with_enhancements(STEEL=16)
        base = 5.0
        adjusted = _deck_composition_adjustment("j_drivers_license", base, dp, None)
        assert adjusted == base

    def test_unrelated_joker_unaffected(self):
        dp = self._profile_with_enhancements(STEEL=10)
        base = 4.0
        adjusted = _deck_composition_adjustment("j_joker", base, dp, None)
        assert adjusted == base

    def test_steel_joker_moved_to_sim(self):
        """Steel Joker's deck-count boost is now a sim-input (projected xmult)."""
        dp = self._profile_with_enhancements(STEEL=8)
        base = 5.0
        adjusted = _deck_composition_adjustment("j_steel_joker", base, dp, None)
        assert adjusted == base


class TestPhase3DensityAwareSim:
    """Phase 3 (issue #35): deck-density-aware synthetic hands.

    Replaces the Steel/Lucky/Glass/suit-enhancement branches of
    ``_deck_composition_adjustment`` with sim inputs — scoring cards carry
    density-planned enhancements; Steel & Glass get xmult projected from deck
    counts before the sim runs."""

    _HL = {h: {"level": 1, "chips": c, "mult": m, "played": 0} for h, c, m in [
        ("High Card", 5, 1), ("Pair", 10, 2), ("Two Pair", 20, 2),
        ("Three of a Kind", 30, 3), ("Straight", 30, 4), ("Flush", 35, 4),
        ("Full House", 40, 4), ("Four of a Kind", 60, 7),
    ]}

    def _deck(self, **enh_counts):
        cards = []
        for enh, cnt in enh_counts.items():
            for _ in range(cnt):
                cards.append(card("A", "H", enhancement=enh))
        for _ in range(52 - len(cards)):
            cards.append(card("2", "S"))
        return DeckProfile.from_cards(cards)

    def _vanilla(self, rank_counts: dict[str, int] | None = None):
        rc = rank_counts or {r: 4 for r in "23456789TJQKA"}
        return DeckProfile(
            total_cards=sum(rc.values()),
            rank_counts=rc,
            suit_counts={s: sum(rc.values()) // 4 for s in "HDCS"},
        )

    def _cand(self, key, effect, rarity=2):
        return {"key": key, "label": key,
                "value": {"effect": effect, "rarity": rarity},
                "cost": {"buy": 8, "sell": 4}}

    # --- Rank density: Wee, Scholar ---

    def test_wee_scales_with_twos_in_deck(self):
        cand = self._cand("j_wee", "+8 Chips, +1 Chip if scored 2", rarity=1)
        baseline = evaluate_joker_value(cand, [], self._HL, ante=2,
                                         deck_profile=self._vanilla())  # 4 twos
        heavy = {r: 4 for r in "3456789TJQKA"}
        heavy["2"] = 8
        heavy_v = evaluate_joker_value(cand, [], self._HL, ante=2,
                                         deck_profile=self._vanilla(heavy))
        light = {r: 4 for r in "3456789TJQKA"}
        light["2"] = 1
        light_v = evaluate_joker_value(cand, [], self._HL, ante=2,
                                         deck_profile=self._vanilla(light))
        assert heavy_v > baseline, f"8 twos should beat 4 twos: {baseline} → {heavy_v}"
        assert light_v < baseline, f"1 two should trail 4 twos: {baseline} → {light_v}"

    def test_scholar_scales_with_ace_density(self):
        cand = self._cand("j_scholar", "Aces give +20 Chips and +4 Mult", rarity=1)
        baseline = evaluate_joker_value(cand, [], self._HL, ante=2,
                                         deck_profile=self._vanilla())
        heavy = {r: 4 for r in "23456789TJQK"}
        heavy["A"] = 8
        heavy_v = evaluate_joker_value(cand, [], self._HL, ante=2,
                                         deck_profile=self._vanilla(heavy))
        assert heavy_v > baseline, f"8 aces should beat 4: {baseline} → {heavy_v}"

    # --- Steel Joker: sim-derived xmult projection ---

    def test_steel_joker_sim_value_scales_with_deck(self):
        cand = self._cand("j_steel_joker", "X1 Mult")
        no_steel = evaluate_joker_value(cand, [], self._HL, ante=2,
                                         deck_profile=self._deck())
        with_steel = evaluate_joker_value(cand, [], self._HL, ante=2,
                                            deck_profile=self._deck(STEEL=8))
        assert with_steel > no_steel, (
            f"Steel Joker w/ 8 Steel cards should beat empty: {no_steel} → {with_steel}"
        )

    # --- Lucky Cat: per-card triggers fire inside the sim ---

    def test_lucky_cat_fires_with_lucky_cards_in_deck(self):
        """With Lucky cards planted on scoring slots, the sim layer produces a
        positive delta (the pre-Phase 3 sim with plain scoring cards returned
        exactly 0 for Lucky Cat regardless of deck density)."""
        cand = self._cand("j_lucky_cat", "X1.5 Mult, +X0.25 per Lucky trigger")
        ctx = _ctx(candidate=cand, ante=2)
        # No Lucky in deck → sim picks no Lucky scoring slots → delta 0
        no_lucky_ctx = SimContext.build(
            candidate=cand, owned_jokers=[], hand_levels=self._HL,
            strategy=ctx.strategy, ante=2, deck_profile=self._deck(),
        )
        # 4 Lucky in deck → _plan_enhancements forces at least one Lucky
        # scoring slot → Lucky Cat's per-card effect fires in the sim
        with_lucky_ctx = SimContext.build(
            candidate=cand, owned_jokers=[], hand_levels=self._HL,
            strategy=ctx.strategy, ante=2, deck_profile=self._deck(LUCKY=4),
        )
        d_none = _scoring_delta(no_lucky_ctx, [("Flush", 1.0)])
        d_lucky = _scoring_delta(with_lucky_ctx, [("Flush", 1.0)])
        assert d_lucky > d_none, (
            f"Lucky Cat sim delta w/ 4 Lucky should beat 0: {d_none} → {d_lucky}"
        )


class TestHeldCardJokerValuation:
    """Held-phase jokers (Baron, Shoot the Moon, Raised Fist, Mime, Blackboard)
    are driven by _synthetic_held_cards in _scoring_delta (Phase 2).
    These tests pin the end-to-end evaluate_joker_value behavior."""

    _HL = {h: {"level": 1, "chips": c, "mult": m} for h, c, m in [
        ("High Card", 5, 1), ("Pair", 10, 2), ("Two Pair", 20, 2),
        ("Three of a Kind", 30, 3), ("Straight", 30, 4), ("Flush", 35, 4),
        ("Full House", 40, 4), ("Four of a Kind", 60, 7),
    ]}
    _VANILLA = DeckProfile(
        total_cards=52,
        rank_counts={r: 4 for r in "23456789TJQKA"},
        suit_counts={s: 13 for s in "HDCS"},
    )

    def _cand(self, key, effect, rarity=2):
        return {"key": key, "label": key,
                "value": {"effect": effect, "rarity": rarity},
                "cost": {"buy": 8, "sell": 4}}

    def test_baron_empty_roster_exceeds_floor(self):
        cand = self._cand("j_baron", "Each King held in hand gives X1.5 Mult")
        v = evaluate_joker_value(cand, [], self._HL, ante=3, deck_profile=self._VANILLA)
        assert v > 6.0, f"Baron empty-roster @ ante 3 should be >6.0, got {v}"

    def test_shoot_the_moon_empty_roster_exceeds_floor(self):
        cand = self._cand("j_shoot_the_moon", "Each Queen held in hand gives +13 Mult")
        v = evaluate_joker_value(cand, [], self._HL, ante=3, deck_profile=self._VANILLA)
        assert v > 3.5, f"SttM empty-roster @ ante 3 should be >3.5, got {v}"

    def test_raised_fist_empty_roster_exceeds_floor(self):
        cand = self._cand("j_raised_fist", "Adds 2x the rank of lowest card held in hand")
        v = evaluate_joker_value(cand, [], self._HL, ante=3, deck_profile=self._VANILLA)
        assert v > 3.0, f"RF empty-roster @ ante 3 should be >3.0, got {v}"

    def test_mime_scales_with_held_phase_roster(self):
        """Mime's retriggers light up when Baron+SttM are owned."""
        cand = self._cand("j_mime", "Retrigger all cards held in hand")
        baron = self._cand("j_baron", "Each King held in hand gives X1.5 Mult")
        sttm = self._cand("j_shoot_the_moon", "Each Queen held in hand gives +13 Mult")
        v_empty = evaluate_joker_value(cand, [], self._HL, ante=3, deck_profile=self._VANILLA)
        v_with = evaluate_joker_value(cand, [baron, sttm], self._HL, ante=3, deck_profile=self._VANILLA)
        assert v_with > 2 * max(v_empty, 0.5), (
            f"Mime value should scale ≥2× with Baron+SttM held, got {v_empty} → {v_with}"
        )

    def test_blackboard_scales_with_sc_deck_density(self):
        cand = self._cand("j_blackboard", "X3 Mult if all held cards are Spades/Clubs")
        heavy = DeckProfile(
            total_cards=52,
            rank_counts={r: 4 for r in "23456789TJQKA"},
            suit_counts={"H": 8, "D": 8, "S": 18, "C": 18},  # 36 S+C
        )
        v_vanilla = evaluate_joker_value(cand, [], self._HL, ante=3, deck_profile=self._VANILLA)
        v_heavy = evaluate_joker_value(cand, [], self._HL, ante=3, deck_profile=heavy)
        assert v_heavy >= 3.0 * v_vanilla, (
            f"Blackboard with 36 S/C should be ≥3× vanilla, got {v_vanilla} → {v_heavy}"
        )


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


class TestPhase4LifetimeProjection:
    """Phase 4 (issue #36): lifetime-aware projection for scaling xmult jokers."""

    _HL = {
        "Pair": {"chips": 10, "mult": 2, "level": 1, "played": 0, "played_this_round": 0},
        "High Card": {"chips": 5, "mult": 1, "level": 1, "played": 0, "played_this_round": 0},
    }

    def _cand(self, key, effect, rarity=2):
        return {"key": key, "label": key,
                "value": {"effect": effect, "rarity": rarity},
                "cost": {"buy": 8, "sell": 4}}

    def test_madness_live_anchor_drives_value(self):
        """Owned Madness at X3.0 Currently should reflect the live anchor."""
        madness = joker("j_madness", "Madness")
        madness["value"] = {
            "effect": "This Joker gains X0.5 Mult when Blind is selected (Currently X3.0 Mult)"
        }
        val = evaluate_joker_value(madness, [], self._HL, ante=3)
        # X3.0 × 5 = 15 via _dynamic_power; projection adds ~X0.75 more
        assert val >= 12.0, f"Madness @ X3.0 should read ≥12, got {val}"

    def test_madness_fresh_candidate_projects_forward(self):
        """Fresh Madness (X1.0) should still get a runway floor from projection."""
        cand = self._cand("j_madness",
                          "This Joker gains X0.5 Mult when Blind is selected (Currently X1.0 Mult)")
        v = evaluate_joker_value(cand, [], self._HL, ante=2)
        # Projection: 0.5 × ~21 blinds × 0.5 = ~5.25 gain → ~X6.25 total → ~31 power
        # Generous bound: far more than sim baseline (~0) thanks to projection.
        assert v > 5.0, f"Fresh Madness should project forward, got {v}"

    def test_yorick_near_proc_outranks_fresh(self):
        """Owned Yorick with 3 cards to proc > fresh Yorick (23 cards to proc).

        Models the roster-scoring path where ``score_roster`` passes each owned
        joker as both candidate and owned, so LifetimeState reads its live text.
        """
        fresh = self._cand("j_yorick",
            "This Joker gains X1.0 Mult per 23 cards discarded, requires 23 more (Currently X1.0 Mult)")
        near = self._cand("j_yorick",
            "This Joker gains X1.0 Mult per 23 cards discarded, requires 3 more (Currently X1.0 Mult)")
        v_fresh = evaluate_joker_value(fresh, [fresh], self._HL, ante=5)
        v_near = evaluate_joker_value(near, [near], self._HL, ante=5)
        assert v_near > v_fresh, f"Near-proc Yorick should out-value fresh: {v_fresh} vs {v_near}"

    def test_campfire_value_positive_at_ante(self):
        """Campfire's projection should produce positive value from this-ante sells."""
        cand = self._cand("j_campfire",
            "This Joker gains X0.25 Mult when a card is sold (Currently X1.0 Mult)")
        v = evaluate_joker_value(cand, [], self._HL, ante=3)
        assert v > 0.5, f"Campfire should project ante-local gain, got {v}"

    def test_constellation_scales_with_planets_used(self):
        """Constellation value should reflect live anchor from effect text."""
        # Anchor at X2.0 Currently — live count captured via effect text.
        owned_constellation = joker("j_constellation", "Constellation")
        owned_constellation["value"] = {
            "effect": "This Joker gains X0.1 Mult per Planet used (Currently X2.0 Mult)"
        }
        v = evaluate_joker_value(owned_constellation, [], self._HL, ante=4)
        # X2.0 × 5 = 10 floor from live anchor alone
        assert v >= 9.0, f"Constellation @ X2.0 live should read ≥9, got {v}"

    def test_throwback_in_registry(self):
        """Throwback should be scoped by SCALING_REGISTRY and project forward."""
        from balatro_bot.scaling import SCALING_REGISTRY
        assert "j_throwback" in SCALING_REGISTRY
        cand = self._cand("j_throwback",
            "X0.25 Mult per Blind skipped this run (Currently X1.0 Mult)")
        v = evaluate_joker_value(cand, [], self._HL, ante=2)
        # Any positive floor from projection, not zero
        assert v > 0.0, f"Throwback should have non-zero projection, got {v}"

    def test_lifetime_state_parses_yorick_remaining(self):
        from balatro_bot.domain.policy.sim_context import LifetimeState
        y = joker("j_yorick", "Yorick")
        y["value"] = {
            "effect": "Gains X1.0 Mult per 23 cards discarded, requires 7 more"
        }
        lt = LifetimeState.from_owned([y])
        assert lt.yorick_cards_to_proc == 7

    def test_lifetime_state_parses_madness_anchor(self):
        from balatro_bot.domain.policy.sim_context import LifetimeState
        m = joker("j_madness", "Madness")
        m["value"] = {"effect": "Currently X4.5 Mult"}
        lt = LifetimeState.from_owned([m])
        assert lt.madness_xmult == 4.5
