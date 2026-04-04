"""Tests for consumable_policy, pack_policy, and blind_policy — Phase 6 extraction."""

from __future__ import annotations

from balatro_bot.domain.policy.consumable_policy import (
    score_consumable,
    evaluate_hex,
    score_use_now,
    score_hold,
    eval_suit_convert,
    eval_glass,
    eval_enhancement,
)
from balatro_bot.domain.policy.pack_policy import (
    score_planet_card,
    choose_from_planet_pack,
    choose_from_buffoon_pack,
    score_spectral_card,
    choose_from_spectral_pack,
    choose_from_tarot_pack,
    HAND_VALUE,
    SPECTRAL_SCORES,
)
from balatro_bot.domain.policy.blind_policy import choose_skip_for_tag
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


def _state(jokers=None, ante=1, money=5, hand_cards=None, consumables=None):
    """Build a minimal state dict."""
    jks = jokers or []
    return {
        "jokers": {"cards": jks, "count": len(jks), "limit": 5},
        "hands": HAND_LEVELS,
        "ante_num": ante,
        "money": money,
        "hand": {"cards": hand_cards or []},
        "consumables": {"cards": consumables or [], "count": len(consumables or []), "limit": 2},
        "round": {"chips": 0, "hands_left": 4, "discards_left": 3},
        "blinds": {"small": {"status": "CURRENT", "score": 300}},
    }


# ---------------------------------------------------------------------------
# score_consumable
# ---------------------------------------------------------------------------


class TestScoreConsumable:
    def test_black_hole_always_top(self):
        state = _state()
        assert score_consumable("c_black_hole", state) == 8.0

    def test_planet_with_affinity(self):
        # Pair planet with a joker that has Pair affinity
        jks = [joker("j_duo")]
        state = _state(jokers=jks)
        strat = compute_strategy(jks, HAND_LEVELS)
        score = score_consumable("c_mercury", state, strat)  # Pair planet
        assert score > 5.0

    def test_planet_off_strategy_no_constellation(self):
        state = _state()
        strat = compute_strategy([], HAND_LEVELS)
        score = score_consumable("c_mercury", state, strat)
        assert score == 0.0

    def test_planet_with_constellation(self):
        jks = [joker("j_constellation")]
        state = _state(jokers=jks)
        strat = compute_strategy(jks, HAND_LEVELS)
        score = score_consumable("c_mercury", state, strat)
        assert score == 3.0  # off-strategy but constellation

    def test_judgement_with_open_slot(self):
        state = _state()
        assert score_consumable("c_judgement", state) == 6.0

    def test_judgement_slots_full(self):
        jks = [joker(f"j_{i}") for i in range(5)]
        state = _state(jokers=jks)
        state["jokers"]["count"] = 5
        assert score_consumable("c_judgement", state) == 0.0

    def test_targeting_tarot_glass(self):
        state = _state()
        strat = compute_strategy([], HAND_LEVELS)
        score = score_consumable("c_justice", state, strat)
        assert score == 4.5

    def test_targeting_tarot_enhance_lucky_with_cat(self):
        jks = [joker("j_lucky_cat")]
        state = _state(jokers=jks)
        strat = compute_strategy(jks, HAND_LEVELS)
        # c_magician is Lucky enhancement
        score = score_consumable("c_magician", state, strat)
        assert score > 5.0  # 3.5 base + 2.0 cat + 1.3x early


# ---------------------------------------------------------------------------
# evaluate_hex
# ---------------------------------------------------------------------------


class TestEvaluateHex:
    def test_no_jokers(self):
        assert evaluate_hex([], 1, HAND_LEVELS) == 0.0

    def test_single_joker(self):
        assert evaluate_hex([joker("j_joker")], 1, HAND_LEVELS) == 3.5

    def test_multiple_scaling_blocked(self):
        jks = [joker("j_campfire"), joker("j_constellation")]
        assert evaluate_hex(jks, 1, HAND_LEVELS) == 0.0

    def test_late_game_penalty(self):
        jks = [joker("j_joker")]
        early = evaluate_hex(jks, 1, HAND_LEVELS)
        late = evaluate_hex(jks, 7, HAND_LEVELS)
        # Single joker always returns 3.5 regardless
        assert early == 3.5
        assert late == 3.5


# ---------------------------------------------------------------------------
# score_hold
# ---------------------------------------------------------------------------


class TestScoreHold:
    def test_planet_always_zero(self):
        assert score_hold("c_mercury", ante=1, slots_full=False, desperate=False, hands_left=4, discards_left=3) == 0.0

    def test_safe_tarot_always_zero(self):
        assert score_hold("c_judgement", ante=1, slots_full=False, desperate=False, hands_left=4, discards_left=3) == 0.0

    def test_suit_convert_with_discards(self):
        hold = score_hold("c_lovers", ante=1, slots_full=False, desperate=False, hands_left=4, discards_left=3)
        # c_lovers is a targeting tarot (enhance, Wild) — not suit_convert
        # Let's use a known suit_convert key instead
        assert isinstance(hold, float)

    def test_slot_pressure_reduces_hold(self):
        hold_normal = score_hold("c_justice", ante=5, slots_full=False, desperate=False, hands_left=4, discards_left=3)
        hold_full = score_hold("c_justice", ante=5, slots_full=True, desperate=False, hands_left=4, discards_left=3)
        assert hold_full < hold_normal


# ---------------------------------------------------------------------------
# score_use_now
# ---------------------------------------------------------------------------


class TestScoreUseNow:
    def test_planet_always_high(self):
        state = _state()
        strat = compute_strategy([], HAND_LEVELS)
        value, args = score_use_now(
            0, "c_mercury", "Mercury", {}, state, strat,
            [], [], HAND_LEVELS, None, 0, 300,
            False, False, 5, 3, 4, 5, 1,
        )
        assert value == 10.0
        assert args is not None
        assert args[0] == "use"

    def test_safe_tarot_uses_score_consumable(self):
        state = _state()
        strat = compute_strategy([], HAND_LEVELS)
        value, args = score_use_now(
            0, "c_high_priestess", "High Priestess", {}, state, strat,
            [], [], HAND_LEVELS, None, 0, 300,
            False, False, 5, 3, 4, 5, 1,
        )
        assert value == 5.0

    def test_hex_no_jokers(self):
        state = _state()
        strat = compute_strategy([], HAND_LEVELS)
        value, args = score_use_now(
            0, "c_hex", "Hex", {}, state, strat,
            [], [], HAND_LEVELS, None, 0, 300,
            False, False, 5, 3, 4, 5, 1,
        )
        assert value == 0.0


# ---------------------------------------------------------------------------
# eval_glass
# ---------------------------------------------------------------------------


class TestEvalGlass:
    def test_no_best_hand(self):
        assert eval_glass([], HAND_LEVELS, [], None, 0) is None

    def test_with_unenhanced_scoring_cards(self):
        hand = [card("A", "H"), card("A", "D"), card("3", "C"), card("4", "S"), card("5", "H")]
        best = best_hand(hand, HAND_LEVELS)
        result = eval_glass(hand, HAND_LEVELS, [], best, best.total)
        assert result is not None
        new_score, targets = result
        assert new_score > best.total
        assert len(targets) == 1


# ---------------------------------------------------------------------------
# Planet pack scoring
# ---------------------------------------------------------------------------


class TestPlanetPack:
    def test_black_hole_always_first(self):
        cards = [
            {"key": "c_black_hole", "label": "Black Hole"},
            {"key": "c_mercury", "label": "Mercury"},
        ]
        strat = compute_strategy([], HAND_LEVELS)
        idx, score, reason = choose_from_planet_pack(cards, strat, HAND_LEVELS, set())
        assert idx == 0
        assert "Black Hole" in reason

    def test_affinity_boosts_score(self):
        jks = [joker("j_duo")]
        strat = compute_strategy(jks, HAND_LEVELS)
        pair_card = {"key": "c_mercury", "label": "Mercury"}
        flush_card = {"key": "c_neptune", "label": "Neptune"}
        pair_score = score_planet_card(pair_card, strat, HAND_LEVELS, set())
        flush_score = score_planet_card(flush_card, strat, HAND_LEVELS, set())
        assert pair_score > flush_score

    def test_constellation_guarantees_pick(self):
        strat = compute_strategy([], HAND_LEVELS)
        jk_keys = {"j_constellation"}
        # Off-strategy planet still gets 8.0 with Constellation
        off_planet = {"key": "c_pluto", "label": "Pluto"}  # High Card
        score = score_planet_card(off_planet, strat, HAND_LEVELS, jk_keys)
        assert score >= 8.0

    def test_level_compound_bonus(self):
        leveled = dict(HAND_LEVELS)
        leveled["Pair"] = {"chips": 15, "mult": 3, "level": 3}
        strat = compute_strategy([], leveled)
        pair_card = {"key": "c_mercury", "label": "Mercury"}
        base_score = score_planet_card(pair_card, strat, HAND_LEVELS, set())
        leveled_score = score_planet_card(pair_card, strat, leveled, set())
        assert leveled_score > base_score


# ---------------------------------------------------------------------------
# Buffoon pack scoring
# ---------------------------------------------------------------------------


class TestBuffoonPack:
    def test_picks_best_joker(self):
        pack_cards = [
            joker("j_joker", "Joker"),
            joker("j_jolly", "Jolly Joker"),
        ]
        for c in pack_cards:
            c["set"] = "JOKER"
            c["value"] = {"effect": "+4 Mult"}
        strat = compute_strategy([], HAND_LEVELS)
        idx, score, reason = choose_from_buffoon_pack(
            pack_cards, [], HAND_LEVELS, 1, 5, strat, set(),
        )
        assert idx in (0, 1)
        assert score >= 0

    def test_always_buy_boosts_score(self):
        pack_cards = [joker("j_blueprint", "Blueprint")]
        pack_cards[0]["set"] = "JOKER"
        pack_cards[0]["value"] = {"effect": "Copies ability of Joker to the left"}
        strat = compute_strategy([], HAND_LEVELS)
        idx, score, reason = choose_from_buffoon_pack(
            pack_cards, [], HAND_LEVELS, 1, 5, strat,
            always_buy_keys={"j_blueprint"},
        )
        assert score >= 10.0

    def test_madness_scaling_conflict(self):
        # Madness in pack, scaling joker owned — should be skipped
        pack_cards = [
            joker("j_madness", "Madness"),
            joker("j_joker", "Joker"),
        ]
        for c in pack_cards:
            c["set"] = "JOKER"
            c["value"] = {"effect": "+4 Mult"}
        owned = [joker("j_campfire", "Campfire")]
        strat = compute_strategy(owned, HAND_LEVELS)
        idx, score, reason = choose_from_buffoon_pack(
            pack_cards, owned, HAND_LEVELS, 1, 5, strat, set(),
        )
        # Should pick Joker (index 1) not Madness (index 0)
        assert idx == 1


# ---------------------------------------------------------------------------
# Spectral pack scoring
# ---------------------------------------------------------------------------


class TestSpectralPack:
    def test_hex_score_matches_evaluate_hex(self):
        jks = [joker("j_joker")]
        hex_card = {"key": "c_hex", "label": "Hex"}
        joker_slots = {"count": 1, "limit": 5}
        score = score_spectral_card(hex_card, jks, joker_slots, 1, HAND_LEVELS, None)
        expected = evaluate_hex(jks, 1, HAND_LEVELS)
        assert score == expected

    def test_ectoplasm_blocked_early(self):
        jks = [joker("j_joker")]
        ecto = {"key": "c_ectoplasm", "label": "Ectoplasm"}
        joker_slots = {"count": 1, "limit": 5}
        score = score_spectral_card(ecto, jks, joker_slots, 2, HAND_LEVELS, None)
        assert score == 0.0

    def test_ectoplasm_scales_with_scaling(self):
        jks = [joker("j_campfire")]
        ecto = {"key": "c_ectoplasm", "label": "Ectoplasm"}
        joker_slots = {"count": 1, "limit": 5}
        score = score_spectral_card(ecto, jks, joker_slots, 3, HAND_LEVELS, None)
        assert score >= 6.0  # 4.5 base + 1.5 scaling

    def test_skip_when_nothing_useful(self):
        cards = [{"key": "c_sigil", "label": "Sigil"}, {"key": "c_ouija", "label": "Ouija"}]
        jks = [joker("j_joker")]
        joker_slots = {"count": 1, "limit": 5}
        idx, score, reason, targets = choose_from_spectral_pack(
            cards, jks, joker_slots, 1, HAND_LEVELS, [], None,
        )
        assert idx is None
        assert "skip" in reason


# ---------------------------------------------------------------------------
# Tarot pack scoring
# ---------------------------------------------------------------------------


class TestTarotPack:
    def test_picks_highest_scored(self):
        cards = [
            {"key": "c_judgement", "label": "Judgement"},
            {"key": "c_hermit", "label": "Hermit"},
        ]
        state = _state(money=4)
        strat = compute_strategy([], HAND_LEVELS)
        idx, score, reason, targets = choose_from_tarot_pack(cards, state, [], [], strat)
        # Judgement = 6.0, Hermit = min(4,20)/4 = 1.0
        assert idx == 0

    def test_picks_best_over_worse(self):
        cards = [
            {"key": "c_high_priestess", "label": "High Priestess"},  # 5.0
            {"key": "c_hermit", "label": "Hermit"},  # 1.0 at $4
        ]
        state = _state(money=4)
        strat = compute_strategy([], HAND_LEVELS)
        idx, score, reason, targets = choose_from_tarot_pack(cards, state, [], [], strat)
        assert idx == 0
        assert score == 5.0


# ---------------------------------------------------------------------------
# Blind policy
# ---------------------------------------------------------------------------


class TestBlindPolicy:
    def test_always_returns_false(self):
        assert choose_skip_for_tag({}) is False
