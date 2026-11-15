"""Hand enumeration, best-hand selection, and discard analysis.
"""

from __future__ import annotations

from itertools import combinations
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from balatro_bot.strategy import CardProtection, Strategy

from balatro_bot.cards import card_facts, card_rank, rank_value
from balatro_bot.constants import HAND_INFO

from balatro_bot.domain.scoring.classify import classify_with_counts, scoring_cards_from_facts
from balatro_bot.domain.scoring.chase import generate_chases
from balatro_bot.domain.scoring import estimate
from balatro_bot.domain.scoring.estimate import HandScorer
from balatro_bot.domain.scoring.play_order import PlayOrderer
from balatro_bot.joker_effects.roster import roster


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


_SUBSETS: dict[tuple[int, int, int], tuple] = {}


def _subsets(n: int, min_select: int, max_select: int) -> tuple:
    """Every play out of an n-card hand, smallest first: (played indices, held indices).

    Held indices run left to right, the order the game scores held cards in.
    """
    key = (n, min_select, max_select)
    subsets = _SUBSETS.get(key)
    if subsets is None:
        subsets = _SUBSETS[key] = tuple(
            (indices, tuple(i for i in range(n) if i not in indices))
            for size in range(min_select, min(max_select, n) + 1)
            for indices in combinations(range(n), size)
        )
    return subsets


def _scored_subsets(
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
    deck_cards: list[dict] | None,
    blind_name: str,
    ox_most_played: str | None,
    idol_rank: str | None,
    idol_suit: str | None,
):
    """Yield (hand_name, indices, cards, scoring_cards, chips, mult, total) per allowed play."""
    n = len(hand_cards)

    # Everything that depends only on the hand or the lineup is worked out once
    # here, not once per subset.
    facts = [card_facts(c) for c in hand_cards]
    lineup = roster(jokers) if jokers else None
    joker_keys = lineup.active_keys if lineup else frozenset()
    four_fingers = "j_four_fingers" in joker_keys
    has_splash   = "j_splash" in joker_keys
    shortcut     = "j_shortcut" in joker_keys
    smeared      = "j_smeared" in joker_keys
    orderer = PlayOrderer(
        hand_cards, jokers,
        idol_rank=idol_rank, idol_suit=idol_suit, ancient_suit=ancient_suit,
        lineup=lineup,
    ) if jokers else None

    scorer = HandScorer(
        hand_levels, jokers,
        money=money, discards_left=discards_left, hands_left=hands_left,
        joker_limit=joker_limit, ancient_suit=ancient_suit,
        deck_count=deck_count, deck_cards=deck_cards,
        blind_name=blind_name,
        # The Ox: use the game's locked hand directly from API
        ox_most_played=ox_most_played if blind_name == "The Ox" else None,
        idol_rank=idol_rank, idol_suit=idol_suit,
        lineup=lineup,
    )
    if not all(f.cached for f in facts):
        scorer.use_facts(hand_cards, facts)

    for indices, held_indices in _subsets(n, min_select, max_select):
        if required_card_indices and not required_card_indices.issubset(indices):
            continue
        subset_facts = [facts[i] for i in indices]
        hand_name, rank_counts = classify_with_counts(
            subset_facts, four_fingers=four_fingers,
            shortcut=shortcut, smeared=smeared,
        )
        if required_hand and hand_name != required_hand:
            continue
        if excluded_hands and hand_name in excluded_hands:
            continue

        subset = [hand_cards[i] for i in indices]
        if jokers:
            played_in_order = [hand_cards[i] for i in orderer.order(indices)]
            held = [hand_cards[i] for i in held_indices]
        else:
            played_in_order = subset
            held = []

        scoring = subset if has_splash else scoring_cards_from_facts(
            hand_name, subset, subset_facts,
            four_fingers=four_fingers, smeared=smeared, shortcut=shortcut,
            rank_counts=rank_counts)
        chips, mult, total = scorer.score(hand_name, scoring, played_in_order, held)
        yield hand_name, indices, subset, scoring, chips, mult, total


def _kernel_plays(
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
    """The compiled kernel's answer: every allowed play best first, or just the best."""
    from balatro_bot.domain.scoring.kernel import KernelScorer

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


def _rank_key(jokers: list[dict] | None, hand_affinity: dict[str, float] | None):
    """Sort key (lowest = best) over (hand_name, total, card count).

    With jokers, total score is king; without, hand type priority comes first.
    Affinity then card count break ties.
    """
    def key(hand_name: str, total: int, size: int) -> tuple:
        aff = -(hand_affinity.get(hand_name, 0.0)) if hand_affinity else 0.0
        if jokers:
            return (-total, HAND_INFO[hand_name][2], aff, size)
        return (HAND_INFO[hand_name][2], -total, aff, size)
    return key


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
    deck_cards: list[dict] | None = None,
    blind_name: str = "",
    ox_most_played: str | None = None,
    idol_rank: str | None = None,
    idol_suit: str | None = None,
    hand_affinity: dict[str, float] | None = None,
) -> list[HandCandidate]:
    """Enumerate all valid poker hands from the cards in hand, best first.

    ``hand_affinity`` (optional) is a ``{hand_name: weight}`` dict used as a
    tertiary sort tiebreaker — when two candidates score identically, prefer
    the one that matches the roster's strategic plan. Never overrides score.
    """
    if estimate.USE_KERNEL:
        return _kernel_plays(
            False, hand_cards, hand_levels, max_select, min_select, jokers, money,
            discards_left, hands_left, joker_limit, required_hand, required_card_indices,
            ancient_suit, excluded_hands, deck_count, blind_name,
            ox_most_played, idol_rank, idol_suit, hand_affinity,
        )
    candidates = [
        HandCandidate(hand_name, subset, list(indices), scoring, chips, mult, total)
        for hand_name, indices, subset, scoring, chips, mult, total in _scored_subsets(
            hand_cards, hand_levels, max_select, min_select, jokers, money,
            discards_left, hands_left, joker_limit, required_hand, required_card_indices,
            ancient_suit, excluded_hands, deck_count, deck_cards, blind_name,
            ox_most_played, idol_rank, idol_suit,
        )
    ]
    key = _rank_key(jokers, hand_affinity)
    candidates.sort(key=lambda h: key(h.hand_name, h.total, len(h.cards)))
    return candidates


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
    deck_cards: list[dict] | None = None,
    blind_name: str = "",
    ox_most_played: str | None = None,
    idol_rank: str | None = None,
    idol_suit: str | None = None,
    hand_affinity: dict[str, float] | None = None,
) -> HandCandidate | None:
    """Return the single best hand playable from the given cards: enumerate_hands(...)[0].

    Keeps only the best play as it goes: the first one with the lowest rank key,
    which is the play a stable sort of all of them would put first.
    """
    if estimate.USE_KERNEL:
        found = _kernel_plays(
            True, hand_cards, hand_levels, max_select, min_select, jokers, money,
            discards_left, hands_left, joker_limit, required_hand, required_card_indices,
            ancient_suit, excluded_hands, deck_count, blind_name,
            ox_most_played, idol_rank, idol_suit, hand_affinity,
        )
        return found[0] if found else None
    key = _rank_key(jokers, hand_affinity)
    best = None
    best_key = None
    for item in _scored_subsets(
        hand_cards, hand_levels, max_select, min_select, jokers, money,
        discards_left, hands_left, joker_limit, required_hand, required_card_indices,
        ancient_suit, excluded_hands, deck_count, deck_cards, blind_name,
        ox_most_played, idol_rank, idol_suit,
    ):
        item_key = key(item[0], item[6], len(item[2]))
        if best_key is None or item_key < best_key:
            best_key = item_key
            best = item
    if best is None:
        return None
    hand_name, indices, subset, scoring, chips, mult, total = best
    return HandCandidate(hand_name, subset, list(indices), scoring, chips, mult, total)


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
) -> list[ChaseCandidate]:
    """Suggest discard sets that improve toward better hands.

    Returns candidates in generation order — no ranking. Callers are expected
    to rank via expected-value scoring (Monte Carlo) in the policy layer.
    """
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
