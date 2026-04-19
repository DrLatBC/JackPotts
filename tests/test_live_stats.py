"""Issue #41 — live per-run averages plumbed into LifetimeState / SimContext."""

from __future__ import annotations

from balatro_bot.domain.policy.shop_valuation import evaluate_joker_value
from balatro_bot.domain.policy.sim_context import (
    LifetimeState,
    LiveRunStats,
    SimContext,
)


def _levels() -> dict:
    return {
        "Pair":      {"chips": 10, "mult": 2, "level": 1},
        "High Card": {"chips": 5,  "mult": 1, "level": 1},
        "Flush":     {"chips": 35, "mult": 4, "level": 1},
    }


def _joker(key: str, effect: str = "") -> dict:
    return {
        "key": key,
        "label": key,
        "value": {"effect": effect},
        "cost": {"sell": 3},
    }


class TestLifetimeStateOverrides:
    def test_default_rates_when_live_stats_absent(self):
        lt = LifetimeState.from_owned([])
        assert lt.avg_discards_per_round == 1.5
        assert lt.avg_sells_per_ante == 1.5

    def test_live_stats_override_discards(self):
        live = LiveRunStats(avg_discards_per_round=3.2, avg_sells_per_ante=0.4)
        lt = LifetimeState.from_owned([], live_stats=live)
        assert lt.avg_discards_per_round == 3.2
        assert lt.avg_sells_per_ante == 0.4

    def test_live_stats_does_not_break_xmult_parse(self):
        # Live anchor parsing must still work alongside run-rate overrides.
        m = _joker("j_madness", "Currently X4.5 Mult")
        live = LiveRunStats(avg_discards_per_round=2.0, avg_sells_per_ante=0.5)
        lt = LifetimeState.from_owned([m], live_stats=live)
        assert lt.madness_xmult == 4.5
        assert lt.avg_discards_per_round == 2.0


class TestSimContextBuild:
    def test_build_threads_live_stats(self):
        live = LiveRunStats(avg_discards_per_round=4.0, avg_sells_per_ante=0.2)
        ctx = SimContext.build(
            candidate=_joker("j_joker"), owned_jokers=[], hand_levels=_levels(),
            strategy=None, ante=1, live_stats=live,
        )
        assert ctx.lifetime is not None
        assert ctx.lifetime.avg_discards_per_round == 4.0
        assert ctx.lifetime.avg_sells_per_ante == 0.2


class TestEvaluateJokerValueThreading:
    def test_yorick_high_discard_rate_beats_low(self):
        """Yorick procs per 23 discards — higher discard rate → higher projection."""
        cand = _joker(
            "j_yorick",
            "Gains X1.0 Mult per 23 cards discarded, requires 23 more (Currently X1.0 Mult)",
        )
        low = LiveRunStats(avg_discards_per_round=0.5, avg_sells_per_ante=0.5)
        high = LiveRunStats(avg_discards_per_round=3.5, avg_sells_per_ante=0.5)
        v_low = evaluate_joker_value(cand, [], _levels(), ante=2, live_stats=low)
        v_high = evaluate_joker_value(cand, [], _levels(), ante=2, live_stats=high)
        assert v_high > v_low, f"High-discard run should value Yorick more: {v_low} vs {v_high}"

    def test_campfire_high_sell_rate_beats_low(self):
        """Campfire scales with sells — higher sell rate → higher projection."""
        cand = _joker(
            "j_campfire",
            "This Joker gains X0.25 Mult when a card is sold (Currently X1.0 Mult)",
        )
        low = LiveRunStats(avg_discards_per_round=1.5, avg_sells_per_ante=0.1)
        high = LiveRunStats(avg_discards_per_round=1.5, avg_sells_per_ante=3.0)
        v_low = evaluate_joker_value(cand, [], _levels(), ante=3, live_stats=low)
        v_high = evaluate_joker_value(cand, [], _levels(), ante=3, live_stats=high)
        assert v_high > v_low, f"High-sell run should value Campfire more: {v_low} vs {v_high}"
