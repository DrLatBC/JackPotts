"""Derived shop-phase facts — computed once per shop visit.

ShopFacts centralizes the repeated calculations that multiple shop rules
need: current interest, whether slots are full, effective spending budget, etc.
"""

from __future__ import annotations

from dataclasses import dataclass

from balatro_bot.domain.policy.shop import INTEREST_CAP


@dataclass(frozen=True)
class ShopFacts:
    """Read-only summary of shop-relevant economic state."""

    money: int
    ante: int
    joker_count: int
    joker_limit: int
    current_interest: int  # min(money // 5, 5)

    @staticmethod
    def from_state(state: dict) -> ShopFacts:
        money = state.get("money", 0)
        joker_info = state.get("jokers", {})
        return ShopFacts(
            money=money,
            ante=state.get("ante_num", 1),
            joker_count=joker_info.get("count", 0),
            joker_limit=joker_info.get("limit", 5),
            current_interest=min(money // 5, 5),
        )

    @property
    def slots_full(self) -> bool:
        return self.joker_count >= self.joker_limit

    @property
    def has_open_slots(self) -> bool:
        return self.joker_count < self.joker_limit

    @property
    def above_interest_cap(self) -> bool:
        return self.money >= INTEREST_CAP

    def interest_after_spend(self, cost: int) -> int:
        """Interest earned if we spend ``cost`` dollars."""
        return min((self.money - cost) // 5, 5)

    def loses_interest(self, cost: int) -> bool:
        """True if spending ``cost`` would reduce interest income."""
        return self.interest_after_spend(cost) < self.current_interest
