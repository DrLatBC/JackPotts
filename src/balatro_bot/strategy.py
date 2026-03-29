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
    "j_half": (["High Card", "Pair", "Three of a Kind"], 2),
    "j_trousers": (["Two Pair"], 2),

    # Face card jokers
    "j_scary_face": (["Full House"], 1),
    "j_smiley": (["Full House"], 1),
    "j_photograph": (["Full House"], 1),
    "j_sock_and_buskin": (["Full House"], 1),

    # Fibonacci jokers
    "j_fibonacci": (["Straight"], 2),

    # Specific card jokers
    "j_hack": (["Straight"], 2),

    # xmult conditional by state
    "j_blackboard": (["Flush"], 2),

    # Scaling jokers with hand type triggers
    "j_runner": (["Straight"], 1),
    "j_square": (["Two Pair", "Four of a Kind"], 1),

    # Special
    "j_seeing_double": (["Flush"], 2),
    "j_flower_pot": (["Flush"], 3),
}

# Joker key -> (ranks, weight)
# Weight reflects impact: retrigger=5, +mult=3, +chips=2, utility trigger=2
JOKER_RANK_AFFINITY: dict[str, tuple[list[str], int]] = {
    # Retrigger specific ranks (double scoring on these cards)
    "j_hack":       (["2", "3", "4", "5"], 5),
    "j_fibonacci":  (["A", "2", "3", "5", "8"], 5),

    # +mult on rank group
    "j_even_steven": (["2", "4", "6", "8", "T"], 3),
    "j_odd_todd":    (["A", "3", "5", "7", "9"], 2),  # +chips, lower weight

    # Utility triggers on specific ranks
    "j_8_ball":      (["8"], 2),          # score an 8 → tarot
    "j_sixth_sense": (["6"], 2),          # play a 6 → spectral
    "j_wee":         (["2"], 2),          # +chips when 2 scored, scaling

    # Anti-affinity for face cards (negative weight)
    "j_ride_the_bus": (["J", "Q", "K"], -3),  # resets mult on face cards
}

# Joker key -> (suit, weight)
JOKER_SUIT_AFFINITY: dict[str, tuple[str, int]] = {
    "j_greedy_joker": ("D", 2),
    "j_lusty_joker": ("H", 2),
    "j_wrathful_joker": ("S", 2),
    "j_gluttenous_joker": ("C", 2),
    "j_arrowhead": ("S", 1),
    "j_onyx_agate": ("C", 3),
    "j_bloodstone": ("H", 3),
    "j_rough_gem": ("D", 1),
}


# ---------------------------------------------------------------------------
# Strategy dataclass
# ---------------------------------------------------------------------------

@dataclass
class Strategy:
    """The bot's current strategic focus, derived from owned jokers."""

    preferred_hands: list[tuple[str, float]]
    preferred_suits: list[tuple[str, float]]
    preferred_ranks: list[tuple[str, float]] = field(default_factory=list)

    def top_hand(self) -> str | None:
        return self.preferred_hands[0][0] if self.preferred_hands else None

    def hand_affinity(self, hand_name: str) -> float:
        for name, score in self.preferred_hands:
            if name == hand_name:
                return score
        return 0.0

    def top_suit(self) -> str | None:
        return self.preferred_suits[0][0] if self.preferred_suits else None

    def suit_affinity(self, suit: str) -> float:
        for s, score in self.preferred_suits:
            if s == suit:
                return score
        return 0.0

    def rank_affinity(self, rank: str) -> float:
        for r, score in self.preferred_ranks:
            if r == rank:
                return score
        return 0.0

    def rank_affinity_dict(self) -> dict[str, float]:
        """Return rank affinity as a dict for fast lookup in hot paths."""
        return dict(self.preferred_ranks) if self.preferred_ranks else {}

    def describes(self) -> str:
        parts = []
        if self.preferred_hands:
            top3 = [f"{name}({score:.0f})" for name, score in self.preferred_hands[:3]]
            parts.append("hands=" + ",".join(top3))
        if self.preferred_suits:
            top2 = [f"{s}({score:.0f})" for s, score in self.preferred_suits[:2]]
            parts.append("suits=" + ",".join(top2))
        if self.preferred_ranks:
            top3 = [f"{r}({score:.0f})" for r, score in self.preferred_ranks[:3]]
            parts.append("ranks=" + ",".join(top3))
        return " | ".join(parts) if parts else "no preference"


# ---------------------------------------------------------------------------
# Compute strategy from game state
# ---------------------------------------------------------------------------

def compute_strategy(
    jokers: list[dict],
    hand_levels: dict[str, dict] | None = None,
) -> Strategy:
    hand_scores: dict[str, float] = {}
    suit_scores: dict[str, float] = {}
    rank_scores: dict[str, float] = {}

    for joker in jokers:
        key = joker.get("key", "")

        if key in JOKER_HAND_AFFINITY:
            hand_types, weight = JOKER_HAND_AFFINITY[key]
            for ht in hand_types:
                hand_scores[ht] = hand_scores.get(ht, 0) + weight

        if key in JOKER_SUIT_AFFINITY:
            suit, weight = JOKER_SUIT_AFFINITY[key]
            suit_scores[suit] = suit_scores.get(suit, 0) + weight

        if key in JOKER_RANK_AFFINITY:
            ranks, weight = JOKER_RANK_AFFINITY[key]
            for r in ranks:
                rank_scores[r] = rank_scores.get(r, 0) + weight

    # Synthesize composite hand affinities: jokers that boost sub-hands
    # should also contribute to hands that CONTAIN those sub-hands.
    # Full House = Three of a Kind + Pair
    # Straight Flush = Straight + Flush
    # Flush House = Full House + Flush
    # Flush Five = Five of a Kind + Flush
    composites = {
        "Full House":      [("Three of a Kind", 0.5), ("Pair", 0.5)],
        "Straight Flush":  [("Straight", 0.7), ("Flush", 0.7)],
        "Flush House":     [("Full House", 0.5), ("Flush", 0.5), ("Three of a Kind", 0.3), ("Pair", 0.3)],
        "Flush Five":      [("Five of a Kind", 0.5), ("Flush", 0.5)],
    }
    for composite, components in composites.items():
        bonus = sum(hand_scores.get(sub, 0) * weight for sub, weight in components)
        if bonus > 0:
            hand_scores[composite] = hand_scores.get(composite, 0) + bonus

    if hand_levels:
        for ht, score in hand_scores.items():
            level = hand_levels.get(ht, {}).get("level", 1)
            if level > 1:
                hand_scores[ht] = score * (1.2 ** (level - 1))

    preferred_hands = sorted(
        [(ht, score) for ht, score in hand_scores.items() if score > 0],
        key=lambda x: -x[1],
    )
    preferred_suits = sorted(
        [(s, score) for s, score in suit_scores.items() if score > 0],
        key=lambda x: -x[1],
    )
    # Include negative affinity (anti-affinity) — consumers use it to penalize
    preferred_ranks = sorted(
        [(r, score) for r, score in rank_scores.items() if score != 0],
        key=lambda x: -x[1],
    )

    return Strategy(
        preferred_hands=preferred_hands,
        preferred_suits=preferred_suits,
        preferred_ranks=preferred_ranks,
    )
