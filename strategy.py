"""
Strategy module — derives a coherent game plan from owned jokers and hand levels.

The strategy tells the bot what hand types and suits to favor across all
decisions: hand play, discard targets, joker purchases, planet picks.

Recomputed whenever jokers change (after shop, after pack pick).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any


# ---------------------------------------------------------------------------
# Affinity tables
# ---------------------------------------------------------------------------

# Joker key -> (hand_types, weight)
# Weight reflects impact: +chips=1, +mult=2, xmult=5
JOKER_HAND_AFFINITY: dict[str, tuple[list[str], int]] = {
    # +mult on hand type (weight 2)
    "j_jolly": (["Pair"], 2),
    "j_zany": (["Three of a Kind"], 2),
    "j_mad": (["Two Pair"], 2),
    "j_crazy": (["Straight"], 2),
    "j_droll": (["Flush"], 2),

    # +chips on hand type (weight 1)
    "j_sly": (["Pair"], 1),
    "j_wily": (["Three of a Kind"], 1),
    "j_clever": (["Two Pair"], 1),
    "j_devious": (["Straight"], 1),
    "j_crafty": (["Flush"], 1),

    # xmult on hand type — weight reflects how often the hand actually fires
    "j_duo": (["Pair"], 5),           # Pair is extremely common
    "j_trio": (["Three of a Kind"], 4),
    "j_family": (["Four of a Kind"], 1),  # almost never hits without a dedicated deck
    "j_order": (["Straight"], 4),
    "j_tribe": (["Flush"], 5),

    # Conditional +mult by game state
    "j_half": (["High Card", "Pair", "Three of a Kind"], 2),  # ≤3 cards — High Card is the purest trigger
    "j_trousers": (["Two Pair"], 2),  # scaling +mult on Two Pair

    # Face card jokers — favor Full House (face cards score in trips/pairs)
    # NOT Pair-specific — face cards appear in all hand types
    "j_scary_face": (["Full House"], 1),
    "j_smiley": (["Full House"], 1),
    "j_photograph": (["Full House"], 1),
    "j_sock_and_buskin": (["Full House"], 1),

    # Fibonacci jokers — A/2/3/5/8 favor Straights (low runs)
    "j_fibonacci": (["Straight"], 2),

    # Specific card jokers
    "j_hack": (["Straight"], 2),  # retrigger 2/3/4/5 — straight cards

    # xmult conditional by state — hand-type agnostic, don't bias toward Pair
    "j_blackboard": (["Flush"], 2),  # all held spades/clubs
    # j_acrobat: X3 on final hand — works with ANY hand, no affinity
    # j_card_sharp: X3 repeated hand — works with ANY hand, no affinity

    # Scaling jokers with hand type triggers
    "j_runner": (["Straight"], 1),
    "j_square": (["Two Pair", "Four of a Kind"], 1),  # exactly 4 cards

    # Special
    "j_seeing_double": (["Flush"], 2),  # club + other suit
    "j_flower_pot": (["Flush"], 3),  # all 4 suits
}

# Joker key -> (suit, weight)
JOKER_SUIT_AFFINITY: dict[str, tuple[str, int]] = {
    # +mult per suit (weight 2)
    "j_greedy_joker": ("D", 2),
    "j_lusty_joker": ("H", 2),
    "j_wrathful_joker": ("S", 2),
    "j_gluttenous_joker": ("C", 2),

    # +chips per suit (weight 1)
    "j_arrowhead": ("S", 1),

    # +mult per suit (higher value)
    "j_onyx_agate": ("C", 3),

    # xmult per suit (weight 3 — probabilistic but strong)
    "j_bloodstone": ("H", 3),

    # Economy per suit (weight 1 — indirect value)
    "j_rough_gem": ("D", 1),
}


# ---------------------------------------------------------------------------
# Strategy dataclass
# ---------------------------------------------------------------------------

@dataclass
class Strategy:
    """The bot's current strategic focus, derived from owned jokers."""

    # Hand types ranked by affinity score (highest first)
    preferred_hands: list[tuple[str, float]]
    # Suits ranked by affinity score (highest first)
    preferred_suits: list[tuple[str, float]]

    def top_hand(self) -> str | None:
        """Return the best hand type, or None if no preference."""
        return self.preferred_hands[0][0] if self.preferred_hands else None

    def hand_affinity(self, hand_name: str) -> float:
        """Return the affinity score for a hand type (0 if not preferred)."""
        for name, score in self.preferred_hands:
            if name == hand_name:
                return score
        return 0.0

    def top_suit(self) -> str | None:
        """Return the best suit, or None if no preference."""
        return self.preferred_suits[0][0] if self.preferred_suits else None

    def suit_affinity(self, suit: str) -> float:
        """Return the affinity score for a suit (0 if not preferred)."""
        for s, score in self.preferred_suits:
            if s == suit:
                return score
        return 0.0

    def describes(self) -> str:
        """Short human-readable description of the strategy."""
        parts = []
        if self.preferred_hands:
            top3 = [f"{name}({score:.0f})" for name, score in self.preferred_hands[:3]]
            parts.append("hands=" + ",".join(top3))
        if self.preferred_suits:
            top2 = [f"{s}({score:.0f})" for s, score in self.preferred_suits[:2]]
            parts.append("suits=" + ",".join(top2))
        return " | ".join(parts) if parts else "no preference"


# ---------------------------------------------------------------------------
# Compute strategy from game state
# ---------------------------------------------------------------------------

def compute_strategy(
    jokers: list[dict],
    hand_levels: dict[str, dict] | None = None,
) -> Strategy:
    """
    Derive a Strategy from the current joker loadout and hand levels.

    Jokers contribute affinity to hand types and suits they boost.
    Hand levels amplify affinity (compound growth makes leveled types better).
    """
    hand_scores: dict[str, float] = {}
    suit_scores: dict[str, float] = {}

    for joker in jokers:
        key = joker.get("key", "")

        # Hand type affinity
        if key in JOKER_HAND_AFFINITY:
            hand_types, weight = JOKER_HAND_AFFINITY[key]
            for ht in hand_types:
                hand_scores[ht] = hand_scores.get(ht, 0) + weight

        # Suit affinity
        if key in JOKER_SUIT_AFFINITY:
            suit, weight = JOKER_SUIT_AFFINITY[key]
            suit_scores[suit] = suit_scores.get(suit, 0) + weight

    # Apply hand level multiplier: each level above 1 multiplies affinity by 1.2x
    if hand_levels:
        for ht, score in hand_scores.items():
            level = hand_levels.get(ht, {}).get("level", 1)
            if level > 1:
                hand_scores[ht] = score * (1.2 ** (level - 1))

    # Sort by score descending, filter out zero
    preferred_hands = sorted(
        [(ht, score) for ht, score in hand_scores.items() if score > 0],
        key=lambda x: -x[1],
    )
    preferred_suits = sorted(
        [(s, score) for s, score in suit_scores.items() if score > 0],
        key=lambda x: -x[1],
    )

    return Strategy(
        preferred_hands=preferred_hands,
        preferred_suits=preferred_suits,
    )
