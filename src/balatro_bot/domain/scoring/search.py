"""Hand enumeration, best-hand selection, and discard analysis.

Moved from hand_evaluator.py during Phase 2 of the logic separation refactor.
"""

from __future__ import annotations

from itertools import combinations
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from balatro_bot.strategy import CardProtection, Strategy

from balatro_bot.cards import card_rank, card_suit, card_suits, is_debuffed, is_joker_debuffed, is_stone, joker_key, rank_value
from balatro_bot.constants import HAND_INFO

from balatro_bot.domain.scoring.classify import classify_hand, _scoring_cards_for
from balatro_bot.domain.scoring.chase import generate_chases
from balatro_bot.domain.scoring.estimate import score_hand


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
    """Enumerate all valid poker hands from the cards in hand.

    ``hand_affinity`` (optional) is a ``{hand_name: weight}`` dict used as a
    tertiary sort tiebreaker — when two candidates score identically, prefer
    the one that matches the roster's strategic plan. Never overrides score.
    """
    candidates: list[HandCandidate] = []
    n = len(hand_cards)
    indices_set = set(range(n))

    joker_keys = {joker_key(j) for j in (jokers or []) if not is_joker_debuffed(j)}
    four_fingers = "j_four_fingers" in joker_keys
    has_splash   = "j_splash" in joker_keys
    shortcut     = "j_shortcut" in joker_keys
    smeared      = "j_smeared" in joker_keys

    from balatro_bot.rules._helpers import _sort_play_order

    # The Ox: use the game's locked hand directly from API
    _ox_mp = ox_most_played if blind_name == "The Ox" else None

    for size in range(min_select, min(max_select, n) + 1):
        for indices in combinations(range(n), size):
            if required_card_indices and not required_card_indices.issubset(set(indices)):
                continue
            subset = [hand_cards[i] for i in indices]
            hand_name = classify_hand(
                subset, four_fingers=four_fingers,
                shortcut=shortcut, smeared=smeared,
            )

            if jokers:
                play_order = _sort_play_order(
                    list(indices), hand_cards, jokers,
                    idol_rank=idol_rank, idol_suit=idol_suit,
                    ancient_suit=ancient_suit,
                )
                played_in_order = [hand_cards[i] for i in play_order]
            else:
                played_in_order = subset

            scoring = subset if has_splash else _scoring_cards_for(hand_name, subset, four_fingers=four_fingers, smeared=smeared, shortcut=shortcut)
            held = [hand_cards[i] for i in indices_set - set(indices)] if jokers else []
            chips, mult, total = score_hand(
                hand_name, scoring, hand_levels,
                jokers=jokers, played_cards=played_in_order, held_cards=held,
                money=money, discards_left=discards_left, hands_left=hands_left,
                joker_limit=joker_limit, ancient_suit=ancient_suit,
                deck_count=deck_count, deck_cards=deck_cards,
                blind_name=blind_name,
                ox_most_played=_ox_mp,
                idol_rank=idol_rank,
                idol_suit=idol_suit,
            )

            candidates.append(HandCandidate(
                hand_name=hand_name,
                cards=subset,
                card_indices=list(indices),
                scoring_cards=scoring,
                chips=chips,
                mult=mult,
                total=total,
            ))

    if required_hand:
        candidates = [c for c in candidates if c.hand_name == required_hand]
    if excluded_hands:
        candidates = [c for c in candidates if c.hand_name not in excluded_hands]

    def _aff(h: HandCandidate) -> float:
        return -(hand_affinity.get(h.hand_name, 0.0)) if hand_affinity else 0.0

    if jokers:
        candidates.sort(key=lambda h: (-h.total, h.priority, _aff(h), len(h.cards)))
    else:
        candidates.sort(key=lambda h: (h.priority, -h.total, _aff(h), len(h.cards)))
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
    """Return the single best hand playable from the given cards."""
    candidates = enumerate_hands(
        hand_cards, hand_levels,
        max_select=max_select, min_select=min_select,
        jokers=jokers, money=money,
        discards_left=discards_left, hands_left=hands_left,
        joker_limit=joker_limit,
        required_hand=required_hand,
        required_card_indices=required_card_indices,
        ancient_suit=ancient_suit,
        excluded_hands=excluded_hands,
        deck_count=deck_count, deck_cards=deck_cards,
        blind_name=blind_name,
        ox_most_played=ox_most_played,
        idol_rank=idol_rank,
        idol_suit=idol_suit,
        hand_affinity=hand_affinity,
    )
    return candidates[0] if candidates else None


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
