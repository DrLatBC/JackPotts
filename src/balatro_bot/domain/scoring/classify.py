"""Hand classification and scoring-card extraction.

The public functions take cards; hand search, which classifies every subset of
one hand, calls ``classify_facts`` / ``scoring_cards_from_facts`` with the
cards' CardFacts so nothing is re-read from a card per subset.
"""

from __future__ import annotations

from balatro_bot.cards import CardFacts, card_facts
from balatro_bot.constants import RANK_ORDER

_FLUSH_HANDS = frozenset({"Flush", "Straight Flush", "Flush Five", "Flush House"})
_SUIT_BITS = (1, 2, 4, 8)


def _rank_counts(facts: list[CardFacts]) -> dict[str, int]:
    """Count occurrences of each rank (ignoring Stone cards)."""
    counts: dict[str, int] = {}
    for f in facts:
        r = f.rank
        if r is not None:
            counts[r] = counts.get(r, 0) + 1
    return counts


def _flush_in(facts: list[CardFacts], smeared: bool = False, four_fingers: bool = False) -> bool:
    """True if enough non-Stone cards share at least one common suit.

    Normally all non-Stone cards must share a suit.  With four_fingers,
    only 4 cards need to share a suit (the 5th can be off-suit).

    Stone cards have no suit and cannot form poker hands, so they are
    excluded from the flush check (same as _straight_in skips them).
    """
    masks = [f.suit_mask_smeared if smeared else f.suit_mask for f in facts if not f.stone]
    if not masks:
        return False
    if not four_fingers:
        common = 15
        for m in masks:
            common &= m
            if not common:
                return False
        return True
    return any(sum(1 for m in masks if m & bit) >= 4 for bit in _SUIT_BITS)


def _has_run(ranks: list[int], min_len: int, max_gap: int) -> bool:
    for start in range(len(ranks) - min_len + 1):
        window = ranks[start:start + min_len]
        if all(window[i + 1] - window[i] <= max_gap for i in range(len(window) - 1)):
            return True
    return False


def _straight_in(facts: list[CardFacts], four_fingers: bool = False, shortcut: bool = False) -> bool:
    """True if the ranked cards form a straight (A-low allowed).

    With shortcut=True, each adjacent pair of ranks can differ by up to 2
    (e.g. 2-4-6-8-T is a valid straight).
    """
    ranks = sorted({f.rank_val for f in facts if f.rank})
    min_len = 4 if four_fingers else 5
    if len(ranks) < min_len:
        return False
    max_gap = 2 if shortcut else 1
    if _has_run(ranks, min_len, max_gap):
        return True
    if 14 in ranks:
        low = sorted([1] + [r for r in ranks if r != 14])
        if len(low) >= min_len and _has_run(low, min_len, max_gap):
            return True
    return False


def _is_straight(cards: list[dict], four_fingers: bool = False, shortcut: bool = False) -> bool:
    return _straight_in([card_facts(c) for c in cards], four_fingers, shortcut=shortcut)


def classify_facts(
    facts: list[CardFacts],
    four_fingers: bool = False,
    shortcut: bool = False,
    smeared: bool = False,
) -> str:
    """classify_hand for cards given as their CardFacts."""
    return classify_with_counts(facts, four_fingers, shortcut, smeared)[0]


def classify_with_counts(
    facts: list[CardFacts],
    four_fingers: bool = False,
    shortcut: bool = False,
    smeared: bool = False,
) -> tuple[str, dict[str, int]]:
    """classify_facts, plus the rank counts it worked from (for scoring_cards_from_facts)."""
    rank_counts = _rank_counts(facts)
    return _classify(facts, rank_counts, four_fingers, shortcut, smeared), rank_counts


def _classify(
    facts: list[CardFacts],
    rank_counts: dict[str, int],
    four_fingers: bool,
    shortcut: bool,
    smeared: bool,
) -> str:
    n = len(facts)
    if n == 0:
        return "High Card"

    counts_sorted = sorted(rank_counts.values(), reverse=True)
    min_sf = 4 if four_fingers else 5
    flush = straight = False
    # A flush or straight needs min_sf non-Stone cards, so smaller plays skip both checks.
    if n >= min_sf and sum(1 for f in facts if not f.stone) >= min_sf:
        flush = _flush_in(facts, smeared=smeared, four_fingers=four_fingers)
        straight = _straight_in(facts, four_fingers, shortcut=shortcut)

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

    if len(counts_sorted) >= 2 and counts_sorted[1] >= 2:
        return "Two Pair"
    if max_kind == 2:
        return "Pair"

    return "High Card"


def classify_hand(
    cards: list[dict],
    four_fingers: bool = False,
    shortcut: bool = False,
    smeared: bool = False,
) -> str:
    """Return the best Balatro hand name for a set of cards."""
    return classify_facts(
        [card_facts(c) for c in cards],
        four_fingers=four_fingers, shortcut=shortcut, smeared=smeared,
    )


# The game tries suits in this order and takes the first one enough cards count as
# (get_flush over Steamodded's suit list).
_FLUSH_SUIT_BITS = (2, 4, 1, 8)  # Diamonds, Clubs, Hearts, Spades


def _four_fingers_flush_cards(cards: list[dict], facts: list[CardFacts], smeared: bool) -> list[dict]:
    """The cards of the flush Four Fingers finds: every non-Stone card of the first
    suit, in the game's suit order, that at least four of them count as."""
    for bit in _FLUSH_SUIT_BITS:
        group = [
            c for c, f in zip(cards, facts)
            if not f.stone and (f.suit_mask_smeared if smeared else f.suit_mask) & bit
        ]
        if len(group) >= 4:
            return group
    return []


def _flush_scoring_cards(
    cards: list[dict], facts: list[CardFacts], smeared: bool = False, four_fingers: bool = False,
) -> list[dict]:
    """Return the non-Stone cards that share the flush suit."""
    suited = [c for c, f in zip(cards, facts) if not f.stone]
    if not suited:
        return []
    if not four_fingers:
        return suited  # all non-Stone cards are flush cards in a normal flush
    return _four_fingers_flush_cards(cards, facts, smeared)


def _straight_scoring_cards(
    cards: list[dict], facts: list[CardFacts], four_fingers: bool = False, shortcut: bool = False,
) -> list[dict] | None:
    """Return only the cards forming the straight (for Four Fingers, 4 cards).

    Picks the highest-value window.  Returns None if no window found.
    """
    min_len = 4 if four_fingers else 5
    max_gap = 2 if shortcut else 1
    ranked = [(f.rank_val, c) for c, f in zip(cards, facts) if f.rank]
    if len(ranked) < min_len:
        return None

    # Deduplicate by rank (keep first card per rank), sort ascending
    by_rank: dict[int, dict] = {}
    for rv, c in ranked:
        by_rank.setdefault(rv, c)
    ranks_sorted = sorted(by_rank.keys())

    best_window: list[int] | None = None
    for start in range(len(ranks_sorted) - min_len + 1):
        window = ranks_sorted[start:start + min_len]
        if all(window[i + 1] - window[i] <= max_gap for i in range(len(window) - 1)):
            best_window = window  # keep going to find highest

    # Ace-low check
    if 14 in by_rank:
        low_ranks = sorted([1] + [r for r in ranks_sorted if r != 14])
        for start in range(len(low_ranks) - min_len + 1):
            window = low_ranks[start:start + min_len]
            if all(window[i + 1] - window[i] <= max_gap for i in range(len(window) - 1)):
                # Ace-low is always the lowest window, only use if no high window found
                if best_window is None:
                    # Map rank 1 back to 14 for card lookup
                    best_window = [14 if r == 1 else r for r in window]

    if best_window is None:
        return None

    window_set = set(best_window)
    # All cards whose rank falls in the window score (including duplicate ranks)
    result = [c for rv, c in ranked if rv in window_set]
    _append_stones(result, cards, facts)
    return result


def _append_stones(result: list[dict], cards: list[dict], facts: list[CardFacts]) -> None:
    """Stone cards always score: add the ones not already in result."""
    for c, f in zip(cards, facts):
        if f.stone and not any(c is r for r in result):
            result.append(c)


def scoring_cards_from_facts(
    hand_name: str, cards: list[dict], facts: list[CardFacts],
    four_fingers: bool = False, smeared: bool = False,
    shortcut: bool = False,
    rank_counts: dict[str, int] | None = None,
) -> list[dict]:
    """_scoring_cards_for with the cards' CardFacts supplied (same order as cards).

    *rank_counts* is classify_with_counts' second result when the caller has it.
    """
    if hand_name in _FLUSH_HANDS:
        if four_fingers:
            # Flush House / Flush Five: all cards are part of the hand structure.
            if hand_name in ("Flush House", "Flush Five"):
                return list(cards)
            # Straight Flush: union of flush-subset and straight-subset.
            # A card scores if it contributed to EITHER the flush or the straight.
            if hand_name == "Straight Flush":
                straight_cards = (
                    _straight_scoring_cards(cards, facts, four_fingers=False, shortcut=shortcut)
                    or _straight_scoring_cards(cards, facts, four_fingers=True, shortcut=shortcut)
                    or []
                )
                # Add any flush-subset cards not already in the straight set
                flush_cards = _flush_scoring_cards(cards, facts, smeared=smeared, four_fingers=True)
                for c in flush_cards:
                    if not any(c is s for s in straight_cards):
                        straight_cards.append(c)
                _append_stones(straight_cards, cards, facts)
                return straight_cards or list(cards)
            # Plain Flush with four_fingers: only the flush suit's cards score, plus Stones.
            flush_cards = _four_fingers_flush_cards(cards, facts, smeared)
            if flush_cards:
                return flush_cards + [c for c, f in zip(cards, facts) if f.stone]
        return list(cards)
    if hand_name == "Straight":
        if four_fingers:
            # All 5 cards score when they form a normal straight, even with
            # Four Fingers active.  Only fall back to the 4-card window when
            # the 5th card breaks the run.
            five = _straight_scoring_cards(cards, facts, four_fingers=False, shortcut=shortcut)
            if five:
                return five
            return _straight_scoring_cards(cards, facts, four_fingers=True, shortcut=shortcut) or list(cards)
        return list(cards)

    if hand_name == "High Card":
        result = _highest_card(cards, facts)
    else:
        rc = rank_counts if rank_counts is not None else _rank_counts(facts)
        # Ranks by count, then by rank value, both descending.
        by_count = sorted(rc, key=lambda r: (-rc[r], -RANK_ORDER.get(r, 0)))

        if hand_name == "Five of a Kind":
            result = _of_rank(cards, facts, by_count[0], 5)
        elif hand_name == "Four of a Kind":
            result = _of_rank(cards, facts, next(r for r in by_count if rc[r] >= 4), 4)
        elif hand_name == "Full House":
            trip_rank = next(r for r in by_count if rc[r] >= 3)
            pair_rank = next(r for r in by_count if rc[r] >= 2 and r != trip_rank)
            result = _of_rank(cards, facts, trip_rank, 3) + _of_rank(cards, facts, pair_rank, 2)
        elif hand_name == "Three of a Kind":
            result = _of_rank(cards, facts, next(r for r in by_count if rc[r] >= 3), 3)
        elif hand_name == "Two Pair":
            result = []
            for pr in [r for r in by_count if rc[r] >= 2][:2]:
                result.extend(c for c, f in zip(cards, facts) if f.rank == pr)
            result = result[:4]
        elif hand_name == "Pair":
            result = _of_rank(cards, facts, next(r for r in by_count if rc[r] >= 2), 2)
        else:
            result = _highest_card(cards, facts)

    _append_stones(result, cards, facts)
    return result


def _highest_card(cards: list[dict], facts: list[CardFacts]) -> list[dict]:
    """The first card of the highest rank, or the first card when none has a rank."""
    best = None
    top = -1
    for c, f in zip(cards, facts):
        if f.rank and f.rank_val > top:
            top = f.rank_val
            best = c
    return cards[:1] if best is None else [best]


def _of_rank(cards: list[dict], facts: list[CardFacts], rank: str, limit: int) -> list[dict]:
    """The first *limit* cards of *rank*."""
    return [c for c, f in zip(cards, facts) if f.rank == rank][:limit]


def _scoring_cards_for(
    hand_name: str, cards: list[dict],
    four_fingers: bool = False, smeared: bool = False,
    shortcut: bool = False,
) -> list[dict]:
    """Return the subset of cards that actually score for the hand type.

    Stone cards always score when played (they contribute +50 chips regardless
    of hand type), so they're appended to the result even if they aren't part
    of the poker hand formation.

    With Four Fingers, a Flush/Straight Flush formed by only 4 suited cards
    does not score the off-suit 5th card.
    """
    return scoring_cards_from_facts(
        hand_name, cards, [card_facts(c) for c in cards],
        four_fingers=four_fingers, smeared=smeared, shortcut=shortcut,
    )
