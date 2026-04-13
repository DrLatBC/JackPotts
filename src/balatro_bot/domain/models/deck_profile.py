"""DeckProfile — centralized deck composition summary.

Computed from the full deck (draw pile + hand) once per game tick.
Provides enhancement counts, suit/rank distributions, and cross-referenced
enhancement-by-suit and enhancement-by-rank maps.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from balatro_bot.cards import card_rank, card_suits

if TYPE_CHECKING:
    from balatro_bot.cards import CardLike

ALL_SUITS = ("H", "D", "C", "S")


@dataclass(frozen=True)
class DeckProfile:
    total_cards: int = 0

    # Suit distribution (Wild cards counted in all suits)
    suit_counts: dict[str, int] = field(default_factory=dict)

    # Rank distribution (Stone cards excluded — they have no rank)
    rank_counts: dict[str, int] = field(default_factory=dict)

    # Enhancement counts by type
    enhancement_counts: dict[str, int] = field(default_factory=dict)

    # Total cards with any non-BASE enhancement (for Drivers License)
    enhanced_card_count: int = 0

    # Cross-referenced: suit -> {enhancement -> count}
    enhancements_by_suit: dict[str, dict[str, int]] = field(default_factory=dict)

    # Cross-referenced: rank -> {enhancement -> count}
    enhancements_by_rank: dict[str, dict[str, int]] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Convenience properties
    # ------------------------------------------------------------------

    @property
    def has_drivers_license_threshold(self) -> bool:
        return self.enhanced_card_count >= 16

    @property
    def dominant_suit(self) -> str | None:
        if not self.suit_counts:
            return None
        return max(self.suit_counts, key=self.suit_counts.get)  # type: ignore[arg-type]

    def enhancement_suit_concentration(self, enhancement: str) -> str | None:
        """Which suit has the most cards with *enhancement*?"""
        best_suit = None
        best_count = 0
        for suit, enh_map in self.enhancements_by_suit.items():
            count = enh_map.get(enhancement, 0)
            if count > best_count:
                best_count = count
                best_suit = suit
        return best_suit

    def enhancement_rank_concentration(self, enhancement: str) -> str | None:
        """Which rank has the most cards with *enhancement*?"""
        best_rank = None
        best_count = 0
        for rank, enh_map in self.enhancements_by_rank.items():
            count = enh_map.get(enhancement, 0)
            if count > best_count:
                best_count = count
                best_rank = rank
        return best_rank

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @staticmethod
    def from_cards(cards: list[CardLike]) -> DeckProfile:
        """Build a DeckProfile in a single pass over *cards*."""
        suit_counts: dict[str, int] = defaultdict(int)
        rank_counts: dict[str, int] = defaultdict(int)
        enhancement_counts: dict[str, int] = defaultdict(int)
        enh_by_suit: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        enh_by_rank: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        enhanced_total = 0

        for card in cards:
            # Suits (Wild → all four)
            for s in card_suits(card):
                suit_counts[s] += 1

            # Rank (None for Stone cards)
            rank = card_rank(card)
            if rank is not None:
                rank_counts[rank] += 1

            # Enhancement
            if hasattr(card, "modifier"):
                enh = card.modifier.enhancement
            else:
                mod = card.get("modifier", {})
                if not isinstance(mod, dict):
                    mod = {}
                enh = mod.get("enhancement")

            if enh and enh != "BASE":
                enhancement_counts[enh] += 1
                enhanced_total += 1

                # Cross-reference: enhancement by suit
                if hasattr(card, "value"):
                    suit = card.value.suit
                else:
                    suit = card.get("value", {}).get("suit")
                if suit:
                    enh_by_suit[suit][enh] += 1

                # Cross-reference: enhancement by rank
                if rank is not None:
                    enh_by_rank[rank][enh] += 1

        return DeckProfile(
            total_cards=len(cards),
            suit_counts=dict(suit_counts),
            rank_counts=dict(rank_counts),
            enhancement_counts=dict(enhancement_counts),
            enhanced_card_count=enhanced_total,
            enhancements_by_suit={s: dict(m) for s, m in enh_by_suit.items()},
            enhancements_by_rank={r: dict(m) for r, m in enh_by_rank.items()},
        )
