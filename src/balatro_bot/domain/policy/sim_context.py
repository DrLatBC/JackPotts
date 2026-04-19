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

    # Phase 3: density (fractions summing to ~1.0 per dimension) from deck_profile.
    # Empty dict when deck_profile is None; callers must treat that as "no signal".
    rank_density: dict[str, float] = field(default_factory=dict, repr=False)
    suit_density: dict[str, float] = field(default_factory=dict, repr=False)
    enhancement_density: dict[str, float] = field(default_factory=dict, repr=False)

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
        rank_density: dict[str, float] = {}
        suit_density: dict[str, float] = {}
        enhancement_density: dict[str, float] = {}
        if deck_profile is not None and deck_profile.total_cards > 0:
            total = deck_profile.total_cards
            rank_density = {r: c / total for r, c in deck_profile.rank_counts.items()}
            suit_density = {s: c / total for s, c in deck_profile.suit_counts.items()}
            enhancement_density = {
                e: c / total for e, c in deck_profile.enhancement_counts.items()
            }
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
            rank_density=rank_density,
            suit_density=suit_density,
            enhancement_density=enhancement_density,
        )
