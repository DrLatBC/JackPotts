"""Hand classification and scoring-card extraction.

Moved from hand_evaluator.py during Phase 2 of the logic separation refactor.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from balatro_bot.cards import card_rank, card_suits, is_stone, rank_value
from balatro_bot.constants import HAND_INFO, RANK_ORDER

if TYPE_CHECKING:
    pass


def _rank_counts(cards: list[dict]) -> dict[str, int]:
    """Count occurrences of each rank (ignoring Stone cards)."""
    counts: dict[str, int] = {}
    for c in cards:
        r = card_rank(c)
        if r is not None:
            counts[r] = counts.get(r, 0) + 1
    return counts


def _is_flush(cards: list[dict], smeared: bool = False, four_fingers: bool = False) -> bool:
    """True if enough non-Stone cards share at least one common suit.

    Normally all non-Stone cards must share a suit.  With four_fingers,
    only 4 cards need to share a suit (the 5th can be off-suit).

    Stone cards have no suit and cannot form poker hands, so they are
    excluded from the flush check (same as _is_straight skips them).
    """
    suited = [c for c in cards if not is_stone(c)]
    if not suited:
        return False
    if not four_fingers:
        common = card_suits(suited[0], smeared=smeared)
        for c in suited[1:]:
            common &= card_suits(c, smeared=smeared)
            if not common:
                return False
        return bool(common)
    else:
        suit_counts: dict[str, int] = {}
        for c in suited:
            for s in card_suits(c, smeared=smeared):
                suit_counts[s] = suit_counts.get(s, 0) + 1
        return any(v >= 4 for v in suit_counts.values())


def _is_straight(cards: list[dict], four_fingers: bool = False, shortcut: bool = False) -> bool:
    """True if the ranked cards form a straight (A-low allowed).

    With shortcut=True, each adjacent pair of ranks can differ by up to 2
    (e.g. 2-4-6-8-T is a valid straight).
    """
    ranks = sorted({rank_value(card_rank(c)) for c in cards if card_rank(c)})
    min_len = 4 if four_fingers else 5
    if len(ranks) < min_len:
        return False
    max_gap = 2 if shortcut else 1
    if all(ranks[i + 1] - ranks[i] <= max_gap for i in range(len(ranks) - 1)):
        return True
    if 14 in ranks:
        low = [r for r in ranks if r != 14]
        if len(low) >= min_len - 1 and low[0] <= (3 if shortcut else 2):
            if all(low[i + 1] - low[i] <= max_gap for i in range(len(low) - 1)):
                return True
    return False


def classify_hand(
    cards: list[dict],
    four_fingers: bool = False,
    shortcut: bool = False,
    smeared: bool = False,
) -> str:
    """Return the best Balatro hand name for a set of cards."""
    n = len(cards)
    if n == 0:
        return "High Card"

    rc = _rank_counts(cards)
    counts_sorted = sorted(rc.values(), reverse=True)
    min_sf = 4 if four_fingers else 5
    n_rankable = sum(1 for c in cards if not is_stone(c))
    flush    = _is_flush(cards, smeared=smeared, four_fingers=four_fingers) and n_rankable >= min_sf
    straight = _is_straight(cards, four_fingers, shortcut=shortcut) and n_rankable >= min_sf

    max_kind = counts_sorted[0] if counts_sorted else 0

    if max_kind >= 5:
        if flush:
            return "Flush Five"
        return "Five of a Kind"

    if max_kind == 4:
        return "Four of a Kind"

    if len(counts_sorted) >= 2 and counts_sorted[0] >= 3 and counts_sorted[1] >= 2:
        if flush:
            return "Flush House"
        return "Full House"

    if straight and flush:
        return "Straight Flush"
    if flush:
        return "Flush"
    if straight:
        return "Straight"

    if max_kind == 3:
        return "Three of a Kind"

    pair_count = sum(1 for v in counts_sorted if v >= 2)
    if pair_count >= 2:
        return "Two Pair"
    if pair_count == 1:
        return "Pair"

    return "High Card"


def _scoring_cards_for(
    hand_name: str, cards: list[dict],
    four_fingers: bool = False, smeared: bool = False,
) -> list[dict]:
    """Return the subset of cards that actually score for the hand type.

    Stone cards always score when played (they contribute +50 chips regardless
    of hand type), so they're appended to the result even if they aren't part
    of the poker hand formation.

    With Four Fingers, a Flush/Straight Flush formed by only 4 suited cards
    does not score the off-suit 5th card.
    """
    rc = _rank_counts(cards)
    counts_sorted = sorted(rc.items(), key=lambda x: (-x[1], -rank_value(x[0])))

    if hand_name in ("Flush", "Straight Flush", "Flush Five", "Flush House"):
        if four_fingers:
            suited = [c for c in cards if not is_stone(c)]
            if suited:
                common = card_suits(suited[0], smeared=smeared)
                for c in suited[1:]:
                    common &= card_suits(c, smeared=smeared)
                if not common:
                    suit_groups: dict[str, list[dict]] = {}
                    for c in suited:
                        for s in card_suits(c, smeared=smeared):
                            suit_groups.setdefault(s, []).append(c)
                    flush_suit = max(suit_groups, key=lambda s: len(suit_groups[s]))
                    flush_cards = suit_groups[flush_suit]
                    stone_cards = [c for c in cards if is_stone(c)]
                    return flush_cards + [c for c in stone_cards if c not in flush_cards]
        return list(cards)
    if hand_name == "Straight":
        return list(cards)

    if hand_name == "Five of a Kind":
        target_rank = counts_sorted[0][0]
        result = [c for c in cards if card_rank(c) == target_rank][:5]

    elif hand_name == "Four of a Kind":
        target_rank = next(r for r, cnt in counts_sorted if cnt >= 4)
        result = [c for c in cards if card_rank(c) == target_rank][:4]

    elif hand_name == "Full House":
        trip_rank = next(r for r, cnt in counts_sorted if cnt >= 3)
        pair_rank = next(r for r, cnt in counts_sorted if cnt >= 2 and r != trip_rank)
        trips = [c for c in cards if card_rank(c) == trip_rank][:3]
        pairs = [c for c in cards if card_rank(c) == pair_rank][:2]
        result = trips + pairs

    elif hand_name == "Three of a Kind":
        target_rank = next(r for r, cnt in counts_sorted if cnt >= 3)
        result = [c for c in cards if card_rank(c) == target_rank][:3]

    elif hand_name == "Two Pair":
        pair_ranks = [r for r, cnt in counts_sorted if cnt >= 2][:2]
        result = []
        for pr in pair_ranks:
            result.extend(c for c in cards if card_rank(c) == pr)
        result = result[:4]

    elif hand_name == "Pair":
        target_rank = next(r for r, cnt in counts_sorted if cnt >= 2)
        result = [c for c in cards if card_rank(c) == target_rank][:2]

    else:
        ranked = [c for c in cards if card_rank(c)]
        ranked.sort(key=lambda c: rank_value(card_rank(c)), reverse=True)
        result = ranked[:1] if ranked else cards[:1]

    result_set = set(id(c) for c in result)
    for c in cards:
        if is_stone(c) and id(c) not in result_set:
            result.append(c)

    return result
