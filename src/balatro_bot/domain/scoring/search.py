"""Hand enumeration, best-hand selection, and discard analysis.

Moved from hand_evaluator.py during Phase 2 of the logic separation refactor.
"""

from __future__ import annotations

from itertools import combinations
from typing import NamedTuple

from balatro_bot.cards import card_rank, card_suit, card_suits, is_debuffed, is_joker_debuffed, is_stone, joker_key, rank_value
from balatro_bot.constants import HAND_INFO

from balatro_bot.domain.scoring.classify import classify_hand, _scoring_cards_for
from balatro_bot.domain.scoring.draws import (
    flush_draw,
    flush_draw_quality,
    straight_draw,
    straight_draw_quality,
    two_pair_draw_quality,
    three_kind_draw_quality,
)
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
        labels = [c.get("label", "?") for c in self.cards]
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
) -> list[HandCandidate]:
    """Enumerate all valid poker hands from the cards in hand."""
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
                play_order = _sort_play_order(list(indices), hand_cards, jokers)
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

    if jokers:
        candidates.sort(key=lambda h: (-h.total, h.priority, len(h.cards)))
    else:
        candidates.sort(key=lambda h: (h.priority, -h.total, len(h.cards)))
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
    )
    return candidates[0] if candidates else None


def cards_not_in(
    hand_cards: list[dict], keep_indices: set[int], blackboard: bool = False,
    rank_affinity: dict[str, float] | None = None,
    scoring_suit: str | None = None,
) -> list[int]:
    """Return indices of cards NOT in the keep set — candidates for discard.

    When rank_affinity is provided, high-affinity ranks are protected (sorted
    last) and negative-affinity ranks are prioritized for discard (sorted first).
    When scoring_suit is set (suit restriction bosses), off-suit cards sort earlier.
    """
    candidates = [i for i in range(len(hand_cards)) if i not in keep_indices]
    candidates.sort(key=lambda i: (
        0 if is_debuffed(hand_cards[i]) else 1,
        0 if blackboard and card_suit(hand_cards[i]) in ("H", "D") else 1,
        1 if scoring_suit and scoring_suit in card_suits(hand_cards[i]) else 0 if not scoring_suit else -1,
        rank_affinity.get(card_rank(hand_cards[i]) or "", 0.0) if rank_affinity else 0,
        rank_value(card_rank(hand_cards[i]) or "2"),
    ))
    return candidates


def discard_candidates(
    hand_cards: list[dict],
    hand_levels: dict[str, dict] | None = None,
    max_select: int = 5,
    max_discard: int = 5,
    strategy_affinity: dict[str, float] | None = None,
    deck_cards: list[dict] | None = None,
    chips_remaining: int = 0,
    jokers: list[dict] | None = None,
    required_hand: str | None = None,
) -> list[ChaseCandidate]:
    """Suggest discard sets that improve toward better hands."""
    bh = best_hand(hand_cards, hand_levels, max_select, jokers=jokers, required_hand=required_hand)
    if not bh:
        return [(list(range(min(max_discard, len(hand_cards)))), "no hand found")]

    joker_keys = {joker_key(j) for j in (jokers or [])}
    shortcut = "j_shortcut" in joker_keys
    smeared = "j_smeared" in joker_keys

    from balatro_bot.strategy import compute_strategy
    strat = compute_strategy(jokers or [], hand_levels)
    rank_aff = strat.rank_affinity_dict() or None

    strategies: list[tuple[str, list[int], float, str]] = []

    if chips_remaining > 0 and bh.total < chips_remaining * 0.10:
        keep_indices = bh.card_indices
        n_discard = min(max_discard, len(hand_cards) - len(keep_indices))
        if n_discard > 0:
            strategies.append((
                "redraw",
                keep_indices,
                0.5,
                f"redraw {n_discard} cards ({bh.hand_name} for {bh.total} is hopeless vs {chips_remaining} needed)",
            ))

    strategies.append((
        bh.hand_name,
        bh.card_indices,
        1.0,
        f"keep {bh.hand_name}, discard dead cards",
    ))

    if HAND_INFO["Flush"][2] < HAND_INFO[bh.hand_name][2]:
        if deck_cards:
            fdq = flush_draw_quality(hand_cards, deck_cards, smeared=smeared, rank_affinity=rank_aff)
            if fdq:
                indices, prob, suit = fdq
                strategies.append((
                    "Flush",
                    indices,
                    prob,
                    f"chase Flush ({prob:.0%} to hit, {suit}), discard {len(hand_cards) - len(indices)} cards",
                ))
        else:
            flush_indices = flush_draw(hand_cards, smeared=smeared)
            if flush_indices:
                strategies.append((
                    "Flush",
                    flush_indices,
                    0.5,
                    f"chase Flush, discard {len(hand_cards) - len(flush_indices)} cards",
                ))

    if HAND_INFO["Straight"][2] < HAND_INFO[bh.hand_name][2]:
        if deck_cards:
            sdq = straight_draw_quality(hand_cards, deck_cards, shortcut=shortcut)
            if sdq:
                indices, prob = sdq
                strategies.append((
                    "Straight",
                    indices,
                    prob,
                    f"chase Straight ({prob:.0%} to hit), discard {len(hand_cards) - len(indices)} cards",
                ))
        else:
            straight_indices = straight_draw(hand_cards, shortcut=shortcut)
            if straight_indices:
                strategies.append((
                    "Straight",
                    straight_indices,
                    0.5,
                    f"chase Straight, discard {len(hand_cards) - len(straight_indices)} cards",
                ))

    if deck_cards and HAND_INFO["Two Pair"][2] < HAND_INFO[bh.hand_name][2]:
        tpdq = two_pair_draw_quality(hand_cards, deck_cards, rank_affinity=rank_aff)
        if tpdq:
            indices, prob = tpdq
            strategies.append((
                "Two Pair",
                indices,
                prob,
                f"chase Two Pair ({prob:.0%} to hit), discard {len(hand_cards) - len(indices)} cards",
            ))

    if deck_cards and HAND_INFO["Three of a Kind"][2] < HAND_INFO[bh.hand_name][2]:
        tkdq = three_kind_draw_quality(hand_cards, deck_cards, rank_affinity=rank_aff)
        if tkdq:
            indices, prob = tkdq
            strategies.append((
                "Three of a Kind",
                indices,
                prob,
                f"chase Three of a Kind ({prob:.0%} to hit), discard {len(hand_cards) - len(indices)} cards",
            ))

    if required_hand:
        strategies = [
            (n, k, p, r) for n, k, p, r in strategies
            if n == required_hand or n == "redraw"
        ]

    has_any_affinity = strategy_affinity and any(v > 0 for v in strategy_affinity.values())

    def chase_score(strategy: tuple[str, list[int], float, str]) -> float:
        hand_name, _, prob, _ = strategy
        if hand_name == "redraw":
            return 150 * prob

        chips, mult, _ = HAND_INFO[hand_name]
        base = chips * mult * prob

        if has_any_affinity:
            affinity = strategy_affinity.get(hand_name, 0)
            if affinity > 0:
                base *= (1.0 + affinity * 0.5)
            elif hand_name != bh.hand_name:
                base *= 0.3
        return base

    strategies.sort(key=chase_score, reverse=True)

    results: list[ChaseCandidate] = []
    has_blackboard = any(joker_key(j) == "j_blackboard" for j in (jokers or []))

    for chase_name, keep, prob, reason in strategies:
        to_discard = cards_not_in(hand_cards, set(keep), blackboard=has_blackboard, rank_affinity=rank_aff)[:max_discard]
        if to_discard:
            results.append(ChaseCandidate(to_discard, reason, chase_name, list(keep), prob))

    return results
