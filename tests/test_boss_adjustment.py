"""Phase 7 — boss-blind-aware valuation adjustments."""

from __future__ import annotations

from balatro_bot.domain.policy.boss_adjustment import (
    BossBlindState,
    boss_multiplier,
    shop_blended_multiplier,
)
from balatro_bot.domain.policy.shop_valuation import evaluate_joker_value


def _levels() -> dict:
    return {
        "Pair":      {"chips": 10, "mult": 2, "level": 1},
        "High Card": {"chips": 5,  "mult": 1, "level": 1},
        "Flush":     {"chips": 35, "mult": 4, "level": 1},
    }


def _joker(key: str) -> dict:
    return {
        "key": key,
        "label": key,
        "value": {"effect": ""},
        "cost": {"sell": 3},
    }


class TestBossMultiplier:
    def test_plant_collapses_photograph(self):
        boss = BossBlindState.from_name("The Plant")
        assert boss_multiplier("j_photograph", boss) < 0.1

    def test_plant_spares_non_face_jokers(self):
        boss = BossBlindState.from_name("The Plant")
        assert boss_multiplier("j_joker", boss) == 1.0
        assert boss_multiplier("j_green_joker", boss) == 1.0

    def test_needle_boosts_acrobat(self):
        boss = BossBlindState.from_name("The Needle")
        assert boss_multiplier("j_acrobat", boss) > 1.2

    def test_needle_hurts_per_hand_scalers(self):
        boss = BossBlindState.from_name("The Needle")
        assert boss_multiplier("j_green_joker", boss) < 0.8

    def test_hook_rewards_discard_scalers(self):
        boss = BossBlindState.from_name("The Hook")
        assert boss_multiplier("j_yorick", boss) > 1.1

    def test_manacle_hurts_held_phase(self):
        boss = BossBlindState.from_name("The Manacle")
        assert boss_multiplier("j_baron", boss) < 0.85

    def test_head_boosts_hearts_suit_joker(self):
        boss = BossBlindState.from_name("The Head")
        assert boss_multiplier("j_bloodstone", boss) > 1.0

    def test_head_penalizes_offsuit_joker(self):
        boss = BossBlindState.from_name("The Head")
        assert boss_multiplier("j_onyx_agate", boss) < 0.6

    def test_no_boss_is_identity(self):
        assert boss_multiplier("j_photograph", None) == 1.0
        assert boss_multiplier("j_joker", BossBlindState(name="")) == 1.0

    def test_unknown_boss_name_is_identity(self):
        assert BossBlindState.from_name("Totally Made Up") is None


class TestShopBlended:
    def test_face_joker_pays_small_systemic_penalty(self):
        # Plant ~14% of bosses × ~0 multiplier means Photograph shop value
        # is ~86% of baseline — well under 1.0 but not collapsed.
        m = shop_blended_multiplier("j_photograph")
        assert 0.8 < m < 0.95

    def test_neutral_joker_near_one(self):
        # Joker isn't face/suit/discard/etc. sensitive, so shop blend ≈ 1.
        assert shop_blended_multiplier("j_joker") == 1.0

    def test_yorick_slight_uplift(self):
        # Hook weights 14% at ×1.3 — yorick shop value slightly above 1.0.
        m = shop_blended_multiplier("j_yorick")
        assert m > 1.0


class TestIntegration:
    def test_photograph_plant_drops_value(self):
        base = evaluate_joker_value(
            _joker("j_photograph"), owned_jokers=[], hand_levels=_levels(), ante=2,
        )
        plant = evaluate_joker_value(
            _joker("j_photograph"), owned_jokers=[], hand_levels=_levels(), ante=2,
            blind_name="The Plant",
        )
        assert plant < base * 0.2

    def test_acrobat_needle_lifts_value(self):
        base = evaluate_joker_value(
            _joker("j_acrobat"), owned_jokers=[], hand_levels=_levels(), ante=2,
        )
        needle = evaluate_joker_value(
            _joker("j_acrobat"), owned_jokers=[], hand_levels=_levels(), ante=2,
            blind_name="The Needle",
        )
        assert needle > base
