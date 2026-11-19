"""Hand enumeration, best-hand selection, and discard analysis.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from balatro_bot.strategy import CardProtection, Strategy

from balatro_bot.cards import card_rank, rank_value
from balatro_bot.constants import HAND_INFO
from balatro_bot.domain.scoring.chase import generate_chases
from balatro_bot.domain.scoring.kernel import KernelScorer


class ChaseCandidate(NamedTuple):
    """Enriched discard suggestion with chase metadata for EV calculation."""
    discard_indices: list[int]
    reason: str
    chase_hand: str        # hand type being chased (e.g. "Flush"), or current best
    keep_indices: list[int]
    hit_prob: float


class HandCandidate:
    """A possible hand to play, with scoring details."""

    __slots__ = (
        "hand_name", "cards", "card_indices", "scoring_cards",
        "chips", "mult", "total", "priority",
    )

    def __init__(
        self,
        hand_name: str,
        cards: list[dict],
        card_indices: list[int],
        scoring_cards: list[dict],
        chips: int,
        mult: int,
        total: int,
    ) -> None:
        self.hand_name = hand_name
        self.cards = cards
        self.card_indices = card_indices
        self.scoring_cards = scoring_cards
        self.chips = chips
        self.mult = mult
        self.total = total
        self.priority = HAND_INFO[hand_name][2]

    def __repr__(self) -> str:
        labels = [c.label if hasattr(c, "label") else c.get("label", "?") for c in self.cards]
        return f"HandCandidate({self.hand_name}, total={self.total}, cards={labels})"


def _plays(
    best_only: bool,
    hand_cards: list[dict],
    hand_levels: dict[str, dict] | None,
    max_select: int,
    min_select: int,
    jokers: list[dict] | None,
    money: int,
    discards_left: int,
    hands_left: int,
    joker_limit: int,
    required_hand: str | None,
    required_card_indices: set[int] | None,
    ancient_suit: str | None,
    excluded_hands: set[str] | None,
    deck_count: int,
    blind_name: str,
    ox_most_played: str | None,
    idol_rank: str | None,
    idol_suit: str | None,
    hand_affinity: dict[str, float] | None,
) -> list[HandCandidate]:
    """Every allowed play best first, or just the best, from the compiled kernel."""
    scorer = KernelScorer(
        hand_levels, jokers, money=money, discards_left=discards_left, hands_left=hands_left,
        joker_limit=joker_limit, ancient_suit=ancient_suit, deck_count=deck_count,
        blind_name=blind_name, ox_most_played=ox_most_played,
        idol_rank=idol_rank, idol_suit=idol_suit,
    )
    limits = (max_select, min_select, required_hand, required_card_indices, excluded_hands, hand_affinity)
    if best_only:
        best = scorer.best(hand_cards, *limits)
        found = [] if best is None else [best]
    else:
        found = scorer.search(hand_cards, *limits)
    return [
        HandCandidate(
            hand_name, [hand_cards[i] for i in indices], list(indices),
            [hand_cards[i] for i in scoring], chips, mult, total,
        )
        for hand_name, indices, chips, mult, total, scoring in found
    ]


def enumerate_hands(
    hand_cards: list[dict],
    hand_levels: dict[str, dict] | None = None,
    max_select: int = 5,
    min_select: int = 1,
    jokers: list[dict] | None = None,
    money: int = 0,
    discards_left: int = 0,
    hands_left: int = 1,
    joker_limit: int = 5,
    required_hand: str | None = None,
    required_card_indices: set[int] | None = None,
    ancient_suit: str | None = None,
    excluded_hands: set[str] | None = None,
    deck_count: int = 0,
    blind_name: str = "",
    ox_most_played: str | None = None,
    idol_rank: str | None = None,
    idol_suit: str | None = None,
    hand_affinity: dict[str, float] | None = None,
) -> list[HandCandidate]:
    """Enumerate all valid poker hands from the cards in hand, best first.

    With jokers, total score is king; without, hand type priority comes first.
    ``hand_affinity`` (optional) is a ``{hand_name: weight}`` dict used as a
    tiebreaker after that — when two candidates score identically, prefer the
    one that matches the roster's strategic plan — then fewer cards.
    """
    return _plays(
        False, hand_cards, hand_levels, max_select, min_select, jokers, money,
        discards_left, hands_left, joker_limit, required_hand, required_card_indices,
        ancient_suit, excluded_hands, deck_count, blind_name,
        ox_most_played, idol_rank, idol_suit, hand_affinity,
    )


def best_hand(
    hand_cards: list[dict],
    hand_levels: dict[str, dict] | None = None,
    max_select: int = 5,
    min_select: int = 1,
    jokers: list[dict] | None = None,
    money: int = 0,
    discards_left: int = 0,
    hands_left: int = 1,
    joker_limit: int = 5,
    required_hand: str | None = None,
    required_card_indices: set[int] | None = None,
    ancient_suit: str | None = None,
    excluded_hands: set[str] | None = None,
    deck_count: int = 0,
    blind_name: str = "",
    ox_most_played: str | None = None,
    idol_rank: str | None = None,
    idol_suit: str | None = None,
    hand_affinity: dict[str, float] | None = None,
) -> HandCandidate | None:
    """Return the single best hand playable from the given cards: enumerate_hands(...)[0]."""
    found = _plays(
        True, hand_cards, hand_levels, max_select, min_select, jokers, money,
        discards_left, hands_left, joker_limit, required_hand, required_card_indices,
        ancient_suit, excluded_hands, deck_count, blind_name,
        ox_most_played, idol_rank, idol_suit, hand_affinity,
    )
    return found[0] if found else None


def cards_not_in(
    hand_cards: list[dict], keep_indices: set[int],
    protection: CardProtection | None = None,
) -> list[int]:
    """Return indices of cards NOT in the keep set, sorted discard-first.

    Sorted ascending by `protection.score(card)` — lowest-protection cards
    come first (best candidates to discard). CardProtection consolidates all
    signals: boss suit constraints, rank/suit/enhancement affinity, idol target,
    Blackboard, and debuff status.

    When `protection` is None, falls back to raw rank order (discard lowest first).
    """
    candidates = [i for i in range(len(hand_cards)) if i not in keep_indices]

    if protection is None:
        candidates.sort(key=lambda i: rank_value(card_rank(hand_cards[i]) or "2"))
        return candidates

    candidates.sort(key=lambda i: protection.score(hand_cards[i]))
    return candidates


def discard_candidates(
    hand_cards: list[dict],
    hand_levels: dict[str, dict] | None = None,
    max_select: int = 5,
    max_discard: int = 5,
    deck_cards: list[dict] | None = None,
    chips_remaining: int = 0,
    jokers: list[dict] | None = None,
    required_hand: str | None = None,
    deck_profile=None,
    protection: CardProtection | None = None,
    forced_index: int | None = None,
) -> list[ChaseCandidate]:
    """Suggest discard sets that improve toward better hands.

    Returns candidates in generation order — no ranking. Callers are expected
    to rank via expected-value scoring (Monte Carlo) in the policy layer.

    ``forced_index`` is the hand card that leaves with any discard (Cerulean Bell):
    no candidate keeps it and every candidate discards it, on top of ``max_discard``
    other cards and within the game's limit of five.
    """
    if forced_index is not None:
        others = [i for i in range(len(hand_cards)) if i != forced_index]
        found = discard_candidates(
            [hand_cards[i] for i in others], hand_levels, max_select, min(max_discard, 4),
            deck_cards, chips_remaining, jokers, required_hand, deck_profile, protection,
        )
        return [
            c._replace(
                discard_indices=[forced_index] + [others[i] for i in c.discard_indices],
                keep_indices=[others[i] for i in c.keep_indices],
            )
            for c in found
        ]

    bh = best_hand(hand_cards, hand_levels, max_select, jokers=jokers, required_hand=required_hand)
    if not bh:
        fallback = list(range(min(max_discard, len(hand_cards))))
        return [ChaseCandidate(fallback, "no hand found", "High Card", [], 0.0)]

    from balatro_bot.strategy import compute_strategy
    strat = compute_strategy(jokers or [], hand_levels)
    rank_aff = strat.rank_affinity_dict() or None

    if protection is None:
        protection = strat.card_protection(jokers=jokers)

    strategies = generate_chases(
        hand_cards, bh,
        hand_levels=hand_levels,
        deck_cards=deck_cards,
        jokers=jokers,
        chips_remaining=chips_remaining,
        max_discard=max_discard,
        rank_affinity=rank_aff,
        required_hand=required_hand,
        deck_profile=deck_profile,
    )

    results: list[ChaseCandidate] = []
    for chase_name, keep, prob, reason in strategies:
        to_discard = cards_not_in(hand_cards, set(keep), protection=protection)[:max_discard]
        if to_discard:
            results.append(ChaseCandidate(to_discard, reason, chase_name, list(keep), prob))

    return results
