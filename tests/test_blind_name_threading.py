"""Issue #43 — blind_name reaches mid-round evaluate_joker_value call sites."""

from __future__ import annotations

from balatro_bot.domain.policy.pack_policy import choose_from_buffoon_pack
from balatro_bot.domain.policy.shop_valuation import evaluate_joker_value
from balatro_bot.rules.consumables import UseConsumables
from balatro_bot.strategy import compute_strategy


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
        "cost": {"sell": 3, "buy": 5},
    }


class TestBuffoonPackBlindName:
    def test_plant_collapses_photograph_pick(self):
        """Photograph is near-zero under Plant (faces debuffed)."""
        cards = [_joker("j_photograph"), _joker("j_joker")]
        strat = compute_strategy([], _levels())
        # Baseline (no boss) — Photograph is at least competitive.
        _, base_score, _ = choose_from_buffoon_pack(
            [_joker("j_photograph")], [], _levels(), ante=2, joker_limit=5,
            strat=strat, always_buy_keys=set(),
        )
        # Under Plant, Photograph should collapse.
        _, plant_score, _ = choose_from_buffoon_pack(
            [_joker("j_photograph")], [], _levels(), ante=2, joker_limit=5,
            strat=strat, always_buy_keys=set(), blind_name="The Plant",
        )
        assert plant_score < base_score * 0.5, (
            f"Plant should crush Photograph: base={base_score} plant={plant_score}"
        )
        # And neutral Joker should stay unaffected.
        _, joker_base, _ = choose_from_buffoon_pack(
            [_joker("j_joker")], [], _levels(), ante=2, joker_limit=5,
            strat=strat, always_buy_keys=set(),
        )
        _, joker_plant, _ = choose_from_buffoon_pack(
            [_joker("j_joker")], [], _levels(), ante=2, joker_limit=5,
            strat=strat, always_buy_keys=set(), blind_name="The Plant",
        )
        assert abs(joker_plant - joker_base) < 0.5, (
            "Neutral Joker should not be affected by Plant"
        )

    def test_needle_boosts_acrobat_pick(self):
        strat = compute_strategy([], _levels())
        _, base_score, _ = choose_from_buffoon_pack(
            [_joker("j_acrobat")], [], _levels(), ante=2, joker_limit=5,
            strat=strat, always_buy_keys=set(),
        )
        _, needle_score, _ = choose_from_buffoon_pack(
            [_joker("j_acrobat")], [], _levels(), ante=2, joker_limit=5,
            strat=strat, always_buy_keys=set(), blind_name="The Needle",
        )
        assert needle_score > base_score, (
            f"Needle should lift Acrobat: base={base_score} needle={needle_score}"
        )


class TestHexSelldownBlindName:
    def test_find_hex_target_passes_blind_name(self):
        """_find_hex_target should treat Photograph as near-zero under Plant and
        pick the non-face joker as the survivor."""
        uc = UseConsumables()
        # Edge case: need 2+ jokers for the sell-down path. Use uneditioned jokers.
        photo = _joker("j_photograph")
        neutral = _joker("j_joker")
        # No boss: Photograph is typically valuable; Plant should collapse it so
        # the neutral Joker becomes the hex survivor (higher value under boss).
        no_boss_target = uc._find_hex_target([photo, neutral], _levels(), ante=3)
        plant_target = uc._find_hex_target([photo, neutral], _levels(), ante=3, blind_name="The Plant")
        # Under Plant, Photograph collapses → Joker wins over Photograph.
        assert plant_target["key"] == "j_joker", (
            f"Under Plant, Hex should target Joker over Photograph, got {plant_target['key']}"
        )
        # (No assertion on no_boss_target — either could win by base valuation;
        # this test's focus is the Plant-aware branch.)
        _ = no_boss_target


class TestEvaluateHexBlindName:
    def test_evaluate_hex_accepts_blind_name(self):
        """evaluate_hex should accept blind_name without error and apply it."""
        from balatro_bot.domain.policy.consumable_policy import evaluate_hex
        # Photograph solo under Plant: internal evaluations should see the
        # collapse and may refuse Hex (below threshold). Baseline should stand.
        jokers = [_joker("j_photograph"), _joker("j_joker")]
        base = evaluate_hex(jokers, ante=3, hand_levels=_levels())
        under_plant = evaluate_hex(
            jokers, ante=3, hand_levels=_levels(), blind_name="The Plant",
        )
        # Under Plant the dominant-joker signal weakens for Photograph, so the
        # hex evaluation should be no higher than baseline.
        assert under_plant <= base + 0.01
