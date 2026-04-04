"""Draw detection, probability helpers, and draw-quality evaluation.

Moved from hand_evaluator.py during Phase 2 of the logic separation refactor.
"""

from __future__ import annotations

from math import comb

from balatro_bot.cards import card_rank, card_suits, rank_value


# ---------------------------------------------------------------------------
# Draw detection
# ---------------------------------------------------------------------------

def flush_draw(hand_cards, smeared: bool = False):
    """If 4+ cards share a suit, return their indices."""
    suits_to_indices: dict[str, list[int]] = {}
    for i, card in enumerate(hand_cards):
        for suit in card_suits(card, smeared=smeared):
            if suit not in suits_to_indices:
                suits_to_indices[suit] = []
            if i not in suits_to_indices[suit]:
                suits_to_indices[suit].append(i)

    for suit, indices in suits_to_indices.items():
        if len(indices) >= 4:
            return indices[:4]

    return None


def straight_draw(hand_cards, shortcut: bool = False):
    """If 4 cards are in sequence (or near-sequence with shortcut), return their indices."""
    rank_to_indices: dict[str, list[int]] = {}
    for i, card in enumerate(hand_cards):
        rank = card_rank(card)
        if rank is None:
            continue
        if rank not in rank_to_indices:
            rank_to_indices[rank] = []
        rank_to_indices[rank].append(i)

    rank_keys = sorted(rank_to_indices.keys(), key=rank_value)
    max_gap = 2 if shortcut else 1

    for i in range(len(rank_keys) - 3):
        window = rank_keys[i:i + 4]
        vals = [rank_value(r) for r in window]
        if all(vals[j + 1] - vals[j] <= max_gap for j in range(3)):
            return [rank_to_indices[r][0] for r in window]

    # Ace-low: check for A-2-3-4 (or A-2-4-6 etc. with shortcut)
    if "A" in rank_to_indices or any(rank_value(r) == 14 for r in rank_to_indices):
        low_ranks = [r for r in rank_keys if rank_value(r) <= (7 if shortcut else 5) and rank_value(r) != 14]
        if len(low_ranks) >= 3:
            window = low_ranks[:3]
            vals = [rank_value(r) for r in window]
            if vals[0] <= (3 if shortcut else 2) and all(vals[j + 1] - vals[j] <= max_gap for j in range(2)):
                ace_rank = next(r for r in rank_to_indices if rank_value(r) == 14)
                return [rank_to_indices[ace_rank][0]] + [rank_to_indices[r][0] for r in window]

    return None


# ---------------------------------------------------------------------------
# Draw probability helpers
# ---------------------------------------------------------------------------

def _prob_two_or_more(good: int, deck_size: int, draws: int) -> float:
    """P(draw at least 2 of `good` cards in `draws` from deck of `deck_size`)."""
    if good < 2 or deck_size <= 0 or draws < 2:
        return 0.0
    total = comb(deck_size, draws)
    if total == 0:
        return 0.0
    bad = deck_size - good
    p0 = comb(bad, draws) if bad >= draws else 0
    p1 = comb(good, 1) * comb(bad, draws - 1) if bad >= draws - 1 else 0
    return max(0.0, min(1.0, 1.0 - (p0 + p1) / total))


def _prob_hit(good: int, deck_size: int, draws: int) -> float:
    """P(draw at least 1 of `good` cards in `draws` from deck of `deck_size`)."""
    if deck_size <= 0 or draws <= 0 or good <= 0:
        return 0.0
    if good >= deck_size:
        return 1.0
    p_miss = 1.0
    for i in range(min(draws, deck_size)):
        if deck_size - i <= 0:
            break
        p_miss *= (deck_size - good - i) / (deck_size - i)
        if p_miss <= 0:
            return 1.0
    return max(0.0, min(1.0, 1.0 - p_miss))


# ---------------------------------------------------------------------------
# Draw quality evaluation
# ---------------------------------------------------------------------------

def flush_draw_quality(
    hand_cards: list[dict], deck_cards: list[dict], smeared: bool = False,
    rank_affinity: dict[str, float] | None = None,
) -> tuple[list[int], float, str] | None:
    """If 4+ cards share a suit, return (keep_indices, probability, suit)."""
    suits_to_indices: dict[str, list[int]] = {}
    for i, card in enumerate(hand_cards):
        for suit in card_suits(card, smeared=smeared):
            if suit not in suits_to_indices:
                suits_to_indices[suit] = []
            if i not in suits_to_indices[suit]:
                suits_to_indices[suit].append(i)

    best: tuple[list[int], float, str] | None = None
    best_prob = -1.0

    for suit, indices in suits_to_indices.items():
        if len(indices) < 4:
            continue
        if len(indices) > 4 and rank_affinity:
            indices.sort(
                key=lambda i: (
                    rank_affinity.get(card_rank(hand_cards[i]) or "", 0.0),
                    rank_value(card_rank(hand_cards[i]) or "2"),
                ),
                reverse=True,
            )
        keep = indices[:4]
        cards_to_draw = len(hand_cards) - len(keep)
        suit_in_deck = sum(1 for c in deck_cards if suit in card_suits(c, smeared=smeared))
        deck_size = len(deck_cards)
        prob = _prob_hit(suit_in_deck, deck_size, cards_to_draw)
        if prob > best_prob:
            best_prob = prob
            best = (keep, prob, suit)

    return best


def straight_draw_quality(
    hand_cards: list[dict], deck_cards: list[dict], shortcut: bool = False,
) -> tuple[list[int], float] | None:
    """If 4 cards are in sequence (or near-sequence with shortcut), return (keep_indices, probability)."""
    rank_to_indices: dict[str, list[int]] = {}
    for i, card in enumerate(hand_cards):
        rank = card_rank(card)
        if rank is None:
            continue
        if rank not in rank_to_indices:
            rank_to_indices[rank] = []
        rank_to_indices[rank].append(i)

    rank_values_list = sorted(rank_to_indices.keys(), key=rank_value)
    max_gap = 2 if shortcut else 1

    best: tuple[list[int], float] | None = None
    best_prob = -1.0

    windows: list[list[str]] = []
    for i in range(len(rank_values_list) - 3):
        window = rank_values_list[i:i + 4]
        vals = [rank_value(r) for r in window]
        if all(vals[j + 1] - vals[j] <= max_gap for j in range(3)):
            windows.append(window)

    rank_val_set = {rank_value(r) for r in rank_to_indices}
    if {14, 2, 3, 4} <= rank_val_set:
        val_to_rank = {rank_value(r): r for r in rank_to_indices}
        windows.append([val_to_rank[v] for v in (14, 2, 3, 4)])
    elif shortcut and 14 in rank_val_set:
        val_to_rank = {rank_value(r): r for r in rank_to_indices}
        low_vals = sorted(v for v in rank_val_set if v <= 7 and v != 14)
        if len(low_vals) >= 3 and low_vals[0] <= 3:
            w = low_vals[:3]
            if all(w[j + 1] - w[j] <= max_gap for j in range(2)):
                windows.append([val_to_rank[14]] + [val_to_rank[v] for v in w])

    for window in windows:
        lo = rank_value(window[0])
        hi = rank_value(window[-1])

        keep = [rank_to_indices[r][0] for r in window]
        cards_to_draw = len(hand_cards) - len(keep)

        needed_ranks: set[int] = set()

        if {rank_value(r) for r in window} == {14, 2, 3, 4}:
            needed_ranks.add(5)
        else:
            if lo > 2:
                needed_ranks.add(lo - 1)
            if hi < 14:
                needed_ranks.add(hi + 1)
            if lo == 2 and hi == 5:
                needed_ranks.add(14)
            if lo == 10 and hi == 13:
                needed_ranks.add(14)

        good = sum(
            1 for c in deck_cards
            if card_rank(c) and rank_value(card_rank(c)) in needed_ranks
        )
        deck_size = len(deck_cards)
        prob = _prob_hit(good, deck_size, cards_to_draw)

        if prob > best_prob:
            best_prob = prob
            best = (keep, prob)

    return best


def two_pair_draw_quality(
    hand_cards: list[dict], deck_cards: list[dict],
    rank_affinity: dict[str, float] | None = None,
) -> tuple[list[int], float] | None:
    """If hand has a pair, return (keep_indices, probability) for chasing Two Pair."""
    rank_to_indices: dict[str, list[int]] = {}
    for i, c in enumerate(hand_cards):
        r = card_rank(c)
        if r:
            rank_to_indices.setdefault(r, []).append(i)

    pairs = [(r, idxs) for r, idxs in rank_to_indices.items() if len(idxs) >= 2]
    if not pairs:
        return None

    pair_rank, pair_indices = max(
        pairs,
        key=lambda x: (
            rank_affinity.get(x[0], 0.0) if rank_affinity else 0.0,
            rank_value(x[0]),
        ),
    )
    keep = pair_indices[:2]
    draws = len(hand_cards) - len(keep)

    deck_rank_counts: dict[str, int] = {}
    for c in deck_cards:
        r = card_rank(c)
        if r and r != pair_rank:
            deck_rank_counts[r] = deck_rank_counts.get(r, 0) + 1

    deck_size = len(deck_cards)
    prob = sum(
        _prob_two_or_more(count, deck_size, draws)
        for count in deck_rank_counts.values()
        if count >= 2
    )
    prob = min(1.0, prob)

    if prob <= 0:
        return None
    return (keep, prob)


def three_kind_draw_quality(
    hand_cards: list[dict], deck_cards: list[dict],
    rank_affinity: dict[str, float] | None = None,
) -> tuple[list[int], float] | None:
    """If hand has a pair, return (keep_indices, probability) for chasing Three of a Kind."""
    rank_to_indices: dict[str, list[int]] = {}
    for i, c in enumerate(hand_cards):
        r = card_rank(c)
        if r:
            rank_to_indices.setdefault(r, []).append(i)

    pairs = [(r, idxs) for r, idxs in rank_to_indices.items() if len(idxs) >= 2]
    if not pairs:
        return None

    deck_rank_counts: dict[str, int] = {}
    for c in deck_cards:
        r = card_rank(c)
        if r:
            deck_rank_counts[r] = deck_rank_counts.get(r, 0) + 1

    pair_rank, pair_indices = max(
        pairs,
        key=lambda x: (
            deck_rank_counts.get(x[0], 0),
            rank_affinity.get(x[0], 0.0) if rank_affinity else 0.0,
        ),
    )
    keep = pair_indices[:2]
    draws = len(hand_cards) - len(keep)

    good = deck_rank_counts.get(pair_rank, 0)
    deck_size = len(deck_cards)
    prob = _prob_hit(good, deck_size, draws)

    if prob <= 0:
        return None
    return (keep, prob)
