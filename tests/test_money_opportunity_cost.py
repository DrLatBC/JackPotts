"""Tests for _money_opportunity_cost roster-aware horizon cap.

The opportunity cost of breaking interest only matters if we survive to
collect it. With a thin roster, the 21-round ante 1 horizon dominated
opp_cost and blocked every affordable joker buy — the fix caps effective
rounds by roster strength (3 + joker_count * 4).
"""

from __future__ import annotations

from pytest import approx

from balatro_bot.domain.policy.shop_evaluator import (
    Budget, _money_opportunity_cost, compute_budget,
)


def _budget(ante: int = 1, joker_count: int = 0) -> Budget:
    return compute_budget(money=7, ante=ante, joker_count=joker_count)


# ---------------------------------------------------------------------------
# Horizon cap: effective_rounds = min(rounds_est, 3 + joker_count * 4)
# ---------------------------------------------------------------------------

def test_zero_jokers_caps_horizon_at_3_rounds() -> None:
    """With 0 jokers at ante 1, horizon is capped at 3 rounds, not 21.

    Ante 1 rounds_est = (8-1)*3 = 21. Roster cap = 3 + 0*4 = 3.
    Breaking $1 interest tier at full scoring_aggression (floor 0.2):
      old: 1 * 21 * 0.2 = 4.2   (blocked most buys)
      new: 1 * 3  * 0.2 = 0.6   (leaves room for scoring_value to win)
    """
    budget = _budget(ante=1, joker_count=0)
    # Spending $5 of $7 drops interest tier from $5→$0 (lost=1/round)
    cost = _money_opportunity_cost(
        cost=5, money=7, budget=budget,
        category_aggression=1.0, joker_count=0,
    )
    assert cost == approx(0.6)


def test_full_roster_uses_full_horizon() -> None:
    """With 3+ jokers at ante 1, cap (3 + 3*4 = 15) is still below
    rounds_est (21), so effective_rounds = 15. At 5 jokers → 23, which
    exceeds rounds_est=21, so rounds_est wins.
    """
    # 5 jokers: cap = 23, rounds_est = 21 → uses 21
    budget = _budget(ante=1, joker_count=5)
    cost = _money_opportunity_cost(
        cost=5, money=7, budget=budget,
        category_aggression=1.0, joker_count=5,
    )
    assert cost == 1 * 21 * 0.2  # 4.2


def test_horizon_scales_linearly_with_roster() -> None:
    """Sanity check the scaling formula: 0→3, 1→7, 2→11, 3→15."""
    budget = _budget(ante=1, joker_count=0)

    # lost_per_round=1 ($7→$2 drops interest from 1 to 0), floor=0.2
    def cost_for(jc: int) -> float:
        return _money_opportunity_cost(
            cost=5, money=7, budget=budget,
            category_aggression=1.0, joker_count=jc,
        )

    # Horizon caps: min(21, 3 + jc*4)
    assert cost_for(0) == approx(0.6)
    assert cost_for(1) == approx(1.4)
    assert cost_for(2) == approx(2.2)
    assert cost_for(3) == approx(3.0)
    assert cost_for(4) == approx(3.8)
    # jc=5 hits rounds_est ceiling
    assert cost_for(5) == approx(4.2)


# ---------------------------------------------------------------------------
# Floor behavior: 0.2 minimum cost_factor
# ---------------------------------------------------------------------------

def test_floor_applies_at_full_scoring_aggression() -> None:
    """Even at scoring_aggression=1.0 the floor charges 0.2×. This is the
    existing knob — tune data showed without it, antes 1-2 never banked.
    """
    budget = _budget(ante=1, joker_count=3)
    cost = _money_opportunity_cost(
        cost=5, money=7, budget=budget,
        category_aggression=1.0, joker_count=3,
    )
    # floor=0.2, effective_rounds=15, lost=1
    assert cost == 1 * 15 * 0.2


def test_low_aggression_pays_full_tax() -> None:
    """Speculative buys at low aggression pay 1 - aggression (above floor)."""
    budget = _budget(ante=1, joker_count=3)
    # speculative_aggression=0.3 → cost_factor = max(0.2, 0.7) = 0.7
    cost = _money_opportunity_cost(
        cost=5, money=7, budget=budget,
        category_aggression=0.3, joker_count=3,
    )
    assert cost == 1 * 15 * 0.7


# ---------------------------------------------------------------------------
# No interest broken → zero cost
# ---------------------------------------------------------------------------

def test_no_interest_broken_returns_zero() -> None:
    """If the buy doesn't cross an interest tier, opp_cost is 0."""
    budget = _budget(ante=1, joker_count=0)
    # $3 money, $2 buy → both tiers are $0 (3//5=0, 1//5=0). No loss.
    cost = _money_opportunity_cost(
        cost=2, money=3, budget=budget,
        category_aggression=1.0, joker_count=0,
    )
    assert cost == 0.0


def test_above_interest_cap_no_loss_for_small_buy() -> None:
    """Money above $25 cap: small buys don't reduce tier. Lost = 0."""
    budget = _budget(ante=3, joker_count=3)
    # $30 → $26 still caps at 5 interest. No loss.
    cost = _money_opportunity_cost(
        cost=4, money=30, budget=budget,
        category_aggression=1.0, joker_count=3,
    )
    assert cost == 0.0


# ---------------------------------------------------------------------------
# Regression: the ante 1 "pass on all jokers" scenario
# ---------------------------------------------------------------------------

def test_ante_1_zero_jokers_affords_typical_shop_joker() -> None:
    """The bug: at ante 1 with 0 jokers and ~$7, buying a $5 joker had
    opp_cost=4.2, which exceeded typical shop_value (~4-5), so the bot
    skipped every joker and died in ante 1.

    With the cap, opp_cost=0.6, so a 4.0-value joker at full aggression
    clears (net = 4.0 - 0.6 = 3.4) well above leave_value (~0.35).
    """
    budget = _budget(ante=1, joker_count=0)
    opp_cost = _money_opportunity_cost(
        cost=5, money=7, budget=budget,
        category_aggression=1.0, joker_count=0,
    )
    # Typical ante 1 joker value (e.g. +4 mult joker scored against pair)
    shop_value = 4.0
    net = shop_value * 1.0 - opp_cost
    leave_value = 0.5 * (1.0 - 0.3)  # speculative_agg=0.3 at ante 1
    assert net > leave_value, f"net={net} should beat leave={leave_value}"


def test_ante_1_full_roster_still_pays_interest_tax() -> None:
    """Complementary: once roster is full, interest stacking matters, so
    opp_cost should remain meaningful and cheap utility buys lose to leave.
    """
    budget = _budget(ante=1, joker_count=5)
    opp_cost = _money_opportunity_cost(
        cost=5, money=7, budget=budget,
        category_aggression=1.0, joker_count=5,
    )
    # Weak utility at 2.0 should lose to opp_cost with full roster
    weak_value = 2.0
    net = weak_value * 1.0 - opp_cost  # 2.0 - 4.2 = -2.2
    assert net < 0
