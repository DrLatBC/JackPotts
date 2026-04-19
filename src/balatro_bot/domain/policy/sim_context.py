"""Valuation SimContext — bundles every input the scoring sim needs.

Phase 1 of the valuation refactor (see issue #32 / #33). Replaces the ad-hoc
parameter cascade through ``evaluate_joker_value`` → ``_scoring_delta`` →
``_synthetic_hand`` with one frozen dataclass.

Later phases will populate the currently-empty fields (held_cards, lifetime,
round_state, economy, boss) — this scaffold is plumbing-only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from balatro_bot.cards import joker_key

if TYPE_CHECKING:
    from balatro_bot.domain.models.deck_profile import DeckProfile
    from balatro_bot.strategy import Strategy


@dataclass(frozen=True)
class SimContext:
    candidate: dict
    owned_jokers: tuple[dict, ...]
    hand_levels: dict[str, dict]
    strategy: "Strategy"
    ante: int
    joker_limit: int = 5
    deck_profile: "DeckProfile | None" = None
    unique_planets_used: int = 0

    # Populated incrementally by later phases:
    held_cards: tuple[dict, ...] = ()
    lifetime: object | None = None
    round_state: object | None = None
    economy: object | None = None
    boss: object | None = None

    # Derived
    candidate_key: str = field(default="", repr=False)
    owned_keys: frozenset[str] = field(default_factory=frozenset, repr=False)

    @classmethod
    def build(
        cls,
        *,
        candidate: dict,
        owned_jokers: list[dict],
        hand_levels: dict[str, dict],
        strategy: "Strategy",
        ante: int,
        joker_limit: int = 5,
        deck_profile: "DeckProfile | None" = None,
        unique_planets_used: int = 0,
    ) -> "SimContext":
        return cls(
            candidate=candidate,
            owned_jokers=tuple(owned_jokers),
            hand_levels=hand_levels,
            strategy=strategy,
            ante=ante,
            joker_limit=joker_limit,
            deck_profile=deck_profile,
            unique_planets_used=unique_planets_used,
            candidate_key=candidate.get("key", "") or joker_key(candidate),
            owned_keys=frozenset(joker_key(j) for j in owned_jokers),
        )
