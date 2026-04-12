"""Draw detection, probability helpers, and draw-quality evaluation.

Moved from hand_evaluator.py during Phase 2 of the logic separation refactor.
"""

from __future__ import annotations

from math import comb

from balatro_bot.cards import card_rank, card_suits, is_debuffed, rank_value


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

def flush_draw_quality_loose(
    hand_cards: list[dict], deck_cards: list[dict], smeared: bool = False,
    rank_affinity: dict[str, float] | None = None,
) -> tuple[list[int], float, str] | None:
    """Keep 3 suited cards, need 2 more of the suit from deck.

    Only activates when exactly 3 cards share a suit (not 4+, where
    the tight variant dominates on probability).
    """
    suits_to_indices: dict[str, list[int]] = {}
    for i, card in enumerate(hand_cards):
        for suit in card_suits(card, smeared=smeared):
            suits_to_indices.setdefault(suit, [])
            if i not in suits_to_indices[suit]:
                suits_to_indices[suit].append(i)

    best: tuple[list[int], float, str] | None = None
    best_prob = -1.0

    for suit, indices in suits_to_indices.items():
        if len(indices) < 3 or len(indices) >= 4:
            continue
        indices_sorted = sorted(
            indices,
            key=lambda i: (
                0 if not is_debuffed(hand_cards[i]) else -1,
                rank_affinity.get(card_rank(hand_cards[i]) or "", 0.0) if rank_affinity else 0.0,
                rank_value(card_rank(hand_cards[i]) or "2"),
            ),
            reverse=True,
        )
        keep = indices_sorted[:3]
        cards_to_draw = len(hand_cards) - len(keep)
        suit_in_deck = sum(1 for c in deck_cards if suit in card_suits(c, smeared=smeared))
        deck_size = len(deck_cards)
        prob = _prob_two_or_more(suit_in_deck, deck_size, cards_to_draw)
        if prob > best_prob:
            best_prob = prob
            best = (keep, prob, suit)

    return best


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
        if len(indices) > 4:
            indices.sort(
                key=lambda i: (
                    0 if not is_debuffed(hand_cards[i]) else -1,
                    rank_affinity.get(card_rank(hand_cards[i]) or "", 0.0) if rank_affinity else 0.0,
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

    # Compute P(at least one rank pairs up) using inclusion-exclusion:
    # P(miss all) = product of P(miss rank_i) for each viable rank
    p_miss_all = 1.0
    for count in deck_rank_counts.values():
        if count >= 2:
            p_hit_rank = _prob_two_or_more(count, deck_size, draws)
            p_miss_all *= (1.0 - p_hit_rank)
    prob = 1.0 - p_miss_all

    if prob <= 0:
        return None
    return (keep, prob)


def two_pair_draw_quality_tight(
    hand_cards: list[dict], deck_cards: list[dict],
    rank_affinity: dict[str, float] | None = None,
) -> tuple[list[int], float] | None:
    """Chase Two Pair by keeping pair + best singleton (targeted approach).

    Keeps fewer draw slots than the loose variant, but targets a specific rank.
    """
    rank_to_indices: dict[str, list[int]] = {}
    for i, c in enumerate(hand_cards):
        r = card_rank(c)
        if r:
            rank_to_indices.setdefault(r, []).append(i)

    pairs = [(r, idxs) for r, idxs in rank_to_indices.items() if len(idxs) >= 2]
    singletons = [(r, idxs) for r, idxs in rank_to_indices.items() if len(idxs) == 1]
    if not pairs or not singletons:
        return None

    deck_rank_counts: dict[str, int] = {}
    for c in deck_cards:
        r = card_rank(c)
        if r:
            deck_rank_counts[r] = deck_rank_counts.get(r, 0) + 1

    pair_rank, pair_indices = max(
        pairs,
        key=lambda x: (
            rank_affinity.get(x[0], 0.0) if rank_affinity else 0.0,
            rank_value(x[0]),
        ),
    )

    best_singleton_rank, best_singleton_idxs = max(
        singletons,
        key=lambda x: (
            deck_rank_counts.get(x[0], 0),
            rank_affinity.get(x[0], 0.0) if rank_affinity else 0.0,
            rank_value(x[0]),
        ),
    )

    good = deck_rank_counts.get(best_singleton_rank, 0)
    if good <= 0:
        return None

    keep = pair_indices[:2] + best_singleton_idxs[:1]
    draws = len(hand_cards) - len(keep)
    deck_size = len(deck_cards)
    prob = _prob_hit(good, deck_size, draws)

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
            rank_value(x[0]),
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


# ---------------------------------------------------------------------------
# Extended draw quality functions
# ---------------------------------------------------------------------------

def pair_draw_quality(
    hand_cards: list[dict], deck_cards: list[dict],
    rank_affinity: dict[str, float] | None = None,
) -> tuple[list[int], float] | None:
    """From High Card, chase a Pair.

    Keep the single card whose rank has the most copies in deck (weighted by
    affinity).  Return (keep_indices, probability).
    """
    rank_to_indices: dict[str, list[int]] = {}
    for i, c in enumerate(hand_cards):
        r = card_rank(c)
        if r:
            rank_to_indices.setdefault(r, []).append(i)

    # Only consider singletons — if we already have a pair, this chase is moot
    singletons = [(r, idxs) for r, idxs in rank_to_indices.items() if len(idxs) == 1]
    if not singletons:
        return None

    deck_rank_counts: dict[str, int] = {}
    for c in deck_cards:
        r = card_rank(c)
        if r:
            deck_rank_counts[r] = deck_rank_counts.get(r, 0) + 1

    # Pick the singleton with best combination of deck copies and affinity
    best_rank, best_indices = max(
        singletons,
        key=lambda x: (
            deck_rank_counts.get(x[0], 0),
            rank_affinity.get(x[0], 0.0) if rank_affinity else 0.0,
            rank_value(x[0]),
        ),
    )

    good = deck_rank_counts.get(best_rank, 0)
    if good <= 0:
        return None

    keep = best_indices[:1]
    draws = len(hand_cards) - len(keep)
    deck_size = len(deck_cards)
    prob = _prob_hit(good, deck_size, draws)

    if prob <= 0:
        return None
    return (keep, prob)


def full_house_draw_quality(
    hand_cards: list[dict], deck_cards: list[dict],
    rank_affinity: dict[str, float] | None = None,
) -> tuple[list[int], float] | None:
    """Chase Full House from Three of a Kind or Two Pair.

    From Three of a Kind: keep trips, need any pair from deck (2 of same rank).
    From Two Pair: keep both pairs, need 1 more of either pair rank.
    Returns the better option if both exist.
    """
    rank_to_indices: dict[str, list[int]] = {}
    for i, c in enumerate(hand_cards):
        r = card_rank(c)
        if r:
            rank_to_indices.setdefault(r, []).append(i)

    trips = [(r, idxs) for r, idxs in rank_to_indices.items() if len(idxs) >= 3]
    pairs = [(r, idxs) for r, idxs in rank_to_indices.items() if len(idxs) >= 2]

    deck_rank_counts: dict[str, int] = {}
    for c in deck_cards:
        r = card_rank(c)
        if r:
            deck_rank_counts[r] = deck_rank_counts.get(r, 0) + 1

    deck_size = len(deck_cards)
    best: tuple[list[int], float] | None = None
    best_prob = -1.0

    # Path A: From Three of a Kind — need any pair from deck
    for trip_rank, trip_idxs in trips:
        keep = trip_idxs[:3]
        draws = len(hand_cards) - len(keep)

        # P(at least one rank pairs up) via inclusion-exclusion
        p_miss_all = 1.0
        for r, count in deck_rank_counts.items():
            if r != trip_rank and count >= 2:
                p_miss_all *= (1.0 - _prob_two_or_more(count, deck_size, draws))
        prob = 1.0 - p_miss_all
        if prob > best_prob:
            best_prob = prob
            best = (keep, prob)

    # Path B: From Two Pair — need 1 more of either pair rank
    if len(pairs) >= 2:
        # Pick the two best pairs by affinity
        sorted_pairs = sorted(
            pairs,
            key=lambda x: (
                rank_affinity.get(x[0], 0.0) if rank_affinity else 0.0,
                rank_value(x[0]),
            ),
            reverse=True,
        )
        p1_rank, p1_idxs = sorted_pairs[0]
        p2_rank, p2_idxs = sorted_pairs[1]
        keep = p1_idxs[:2] + p2_idxs[:2]
        draws = len(hand_cards) - len(keep)

        good = deck_rank_counts.get(p1_rank, 0) + deck_rank_counts.get(p2_rank, 0)
        prob = _prob_hit(good, deck_size, draws)
        if prob > best_prob:
            best_prob = prob
            best = (keep, prob)

    if best and best_prob > 0:
        return best
    return None


def full_house_draw_quality_tight(
    hand_cards: list[dict], deck_cards: list[dict],
    rank_affinity: dict[str, float] | None = None,
) -> tuple[list[int], float] | None:
    """Chase Full House from trips by keeping trips + best singleton.

    Targeted approach: keep trips + the singleton with most copies in deck,
    need 1 of that rank to complete the pair. Fewer draws but specific target.
    """
    rank_to_indices: dict[str, list[int]] = {}
    for i, c in enumerate(hand_cards):
        r = card_rank(c)
        if r:
            rank_to_indices.setdefault(r, []).append(i)

    trips = [(r, idxs) for r, idxs in rank_to_indices.items() if len(idxs) >= 3]
    if not trips:
        return None

    trip_ranks = {r for r, _ in trips}
    companions = [(r, idxs) for r, idxs in rank_to_indices.items()
                  if r not in trip_ranks and len(idxs) >= 1]
    if not companions:
        return None

    deck_rank_counts: dict[str, int] = {}
    for c in deck_cards:
        r = card_rank(c)
        if r:
            deck_rank_counts[r] = deck_rank_counts.get(r, 0) + 1

    best: tuple[list[int], float] | None = None
    best_prob = -1.0

    for trip_rank, trip_idxs in trips:
        best_companion_rank, best_companion_idxs = max(
            companions,
            key=lambda x: (
                deck_rank_counts.get(x[0], 0),
                rank_affinity.get(x[0], 0.0) if rank_affinity else 0.0,
                rank_value(x[0]),
            ),
        )

        good = deck_rank_counts.get(best_companion_rank, 0)
        if good <= 0:
            continue

        keep = trip_idxs[:3] + best_companion_idxs[:1]
        draws = len(hand_cards) - len(keep)
        deck_size = len(deck_cards)
        prob = _prob_hit(good, deck_size, draws)

        if prob > best_prob:
            best_prob = prob
            best = (keep, prob)

    if best and best_prob > 0:
        return best
    return None


def four_kind_draw_quality(
    hand_cards: list[dict], deck_cards: list[dict],
    rank_affinity: dict[str, float] | None = None,
) -> tuple[list[int], float] | None:
    """Chase Four of a Kind from Three of a Kind.

    Keep trips, need the 4th copy from deck.
    """
    rank_to_indices: dict[str, list[int]] = {}
    for i, c in enumerate(hand_cards):
        r = card_rank(c)
        if r:
            rank_to_indices.setdefault(r, []).append(i)

    trips = [(r, idxs) for r, idxs in rank_to_indices.items() if len(idxs) >= 3]
    if not trips:
        return None

    deck_rank_counts: dict[str, int] = {}
    for c in deck_cards:
        r = card_rank(c)
        if r:
            deck_rank_counts[r] = deck_rank_counts.get(r, 0) + 1

    # Pick the trip with best deck availability + affinity
    trip_rank, trip_idxs = max(
        trips,
        key=lambda x: (
            deck_rank_counts.get(x[0], 0),
            rank_affinity.get(x[0], 0.0) if rank_affinity else 0.0,
        ),
    )

    good = deck_rank_counts.get(trip_rank, 0)
    if good <= 0:
        return None

    keep = trip_idxs[:3]
    draws = len(hand_cards) - len(keep)
    deck_size = len(deck_cards)
    prob = _prob_hit(good, deck_size, draws)

    if prob <= 0:
        return None
    return (keep, prob)


def five_kind_draw_quality(
    hand_cards: list[dict], deck_cards: list[dict],
) -> tuple[list[int], float] | None:
    """Chase Five of a Kind from Four of a Kind.

    Keep quads, need the 5th copy from deck.
    """
    rank_to_indices: dict[str, list[int]] = {}
    for i, c in enumerate(hand_cards):
        r = card_rank(c)
        if r:
            rank_to_indices.setdefault(r, []).append(i)

    quads = [(r, idxs) for r, idxs in rank_to_indices.items() if len(idxs) >= 4]
    if not quads:
        return None

    deck_rank_counts: dict[str, int] = {}
    for c in deck_cards:
        r = card_rank(c)
        if r:
            deck_rank_counts[r] = deck_rank_counts.get(r, 0) + 1

    quad_rank, quad_idxs = max(
        quads,
        key=lambda x: deck_rank_counts.get(x[0], 0),
    )
    good = deck_rank_counts.get(quad_rank, 0)
    if good <= 0:
        return None

    keep = quad_idxs[:4]
    draws = len(hand_cards) - len(keep)
    deck_size = len(deck_cards)
    prob = _prob_hit(good, deck_size, draws)

    if prob <= 0:
        return None
    return (keep, prob)


def straight_flush_draw_quality(
    hand_cards: list[dict], deck_cards: list[dict],
    shortcut: bool = False, smeared: bool = False,
) -> tuple[list[int], float] | None:
    """Chase Straight Flush from 4 suited sequential cards.

    Need the 5th card that is both the right rank AND the right suit.
    """
    # Build rank-suit index
    rank_to_indices: dict[str, list[int]] = {}
    for i, c in enumerate(hand_cards):
        r = card_rank(c)
        if r:
            rank_to_indices.setdefault(r, []).append(i)

    suits_to_indices: dict[str, list[int]] = {}
    for i, c in enumerate(hand_cards):
        for s in card_suits(c, smeared=smeared):
            suits_to_indices.setdefault(s, [])
            if i not in suits_to_indices[s]:
                suits_to_indices[s].append(i)

    rank_keys = sorted(rank_to_indices.keys(), key=rank_value)
    max_gap = 2 if shortcut else 1

    best: tuple[list[int], float] | None = None
    best_prob = -1.0
    deck_size = len(deck_cards)

    # Find 4-card windows that are both sequential AND share a suit
    for suit, suit_indices in suits_to_indices.items():
        if len(suit_indices) < 4:
            continue

        # Ranks available in this suit
        suit_ranks: dict[str, int] = {}
        for idx in suit_indices:
            r = card_rank(hand_cards[idx])
            if r:
                suit_ranks[r] = idx

        sorted_ranks = sorted(suit_ranks.keys(), key=rank_value)

        windows: list[list[str]] = []
        for i in range(len(sorted_ranks) - 3):
            window = sorted_ranks[i:i + 4]
            vals = [rank_value(r) for r in window]
            if all(vals[j + 1] - vals[j] <= max_gap for j in range(3)):
                windows.append(window)

        # Ace-low check
        rank_val_set = {rank_value(r) for r in sorted_ranks}
        if {14, 2, 3, 4} <= rank_val_set:
            val_to_rank = {rank_value(r): r for r in sorted_ranks}
            windows.append([val_to_rank[v] for v in (14, 2, 3, 4)])

        for window in windows:
            keep = [suit_ranks[r] for r in window]
            draws = len(hand_cards) - len(keep)

            lo = rank_value(window[0])
            hi = rank_value(window[-1])
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

            # Need card matching BOTH the suit and one of the needed ranks
            good = sum(
                1 for c in deck_cards
                if card_rank(c) and rank_value(card_rank(c)) in needed_ranks
                and suit in card_suits(c, smeared=smeared)
            )
            prob = _prob_hit(good, deck_size, draws)

            if prob > best_prob:
                best_prob = prob
                best = (keep, prob)

    if best and best_prob > 0:
        return best
    return None


def flush_house_draw_quality(
    hand_cards: list[dict], deck_cards: list[dict],
    smeared: bool = False, rank_affinity: dict[str, float] | None = None,
) -> tuple[list[int], float] | None:
    """Chase Flush House (flush that is also a Full House).

    Need 5 cards of the same suit that form a Full House (trips + pair).
    From 4 suited cards with a pair or trips among them, need 1 more suited
    card of the right rank.
    """
    suits_to_indices: dict[str, list[int]] = {}
    for i, c in enumerate(hand_cards):
        for s in card_suits(c, smeared=smeared):
            suits_to_indices.setdefault(s, [])
            if i not in suits_to_indices[s]:
                suits_to_indices[s].append(i)

    deck_size = len(deck_cards)
    best: tuple[list[int], float] | None = None
    best_prob = -1.0

    for suit, indices in suits_to_indices.items():
        if len(indices) < 4:
            continue

        # Count ranks within this suit
        suit_rank_indices: dict[str, list[int]] = {}
        for idx in indices:
            r = card_rank(hand_cards[idx])
            if r:
                suit_rank_indices.setdefault(r, []).append(idx)

        # Need a pair or trips within the suited cards
        suit_pairs = {r: idxs for r, idxs in suit_rank_indices.items() if len(idxs) >= 2}
        suit_trips = {r: idxs for r, idxs in suit_rank_indices.items() if len(idxs) >= 3}

        # Path A: Have trips in suit — need a pair in same suit from deck
        for trip_rank, trip_idxs in suit_trips.items():
            keep = trip_idxs[:3]
            # Add the best non-trip suited card (prefer high affinity/rank)
            other_ranks = sorted(
                [(r, idxs) for r, idxs in suit_rank_indices.items() if r != trip_rank],
                key=lambda x: (
                    rank_affinity.get(x[0], 0.0) if rank_affinity else 0.0,
                    rank_value(x[0]),
                ),
                reverse=True,
            )
            if other_ranks:
                keep = keep + other_ranks[0][1][:1]
            if len(keep) < 4:
                continue
            keep = keep[:4]
            draws = len(hand_cards) - len(keep)

            # Need a card in this suit that pairs with a non-trip rank we're keeping
            kept_ranks = [card_rank(hand_cards[i]) for i in keep if card_rank(hand_cards[i]) != trip_rank]
            good = sum(
                1 for c in deck_cards
                if suit in card_suits(c, smeared=smeared)
                and card_rank(c) in kept_ranks
            )
            prob = _prob_hit(good, deck_size, draws)
            if prob > best_prob:
                best_prob = prob
                best = (keep, prob)

        # Path B: Have pair in suit — need trips complement (2 more of a rank in suit)
        for pair_rank, pair_idxs in suit_pairs.items():
            if pair_rank in suit_trips:
                continue  # Already covered by Path A
            keep = pair_idxs[:2]
            # Add other suited cards, preferring high affinity/rank
            other_idxs = sorted(
                [idx for r, idxs in suit_rank_indices.items()
                 if r != pair_rank for idx in idxs if idx not in keep],
                key=lambda idx: (
                    rank_affinity.get(card_rank(hand_cards[idx]) or "", 0.0) if rank_affinity else 0.0,
                    rank_value(card_rank(hand_cards[idx]) or "2"),
                ),
                reverse=True,
            )
            for idx in other_idxs:
                keep.append(idx)
                if len(keep) >= 4:
                    break
            if len(keep) < 4:
                continue
            keep = keep[:4]
            draws = len(hand_cards) - len(keep)

            # Need 1 more of pair_rank in this suit (gives us trips to go with existing pair)
            good = sum(
                1 for c in deck_cards
                if suit in card_suits(c, smeared=smeared)
                and card_rank(c) == pair_rank
            )
            prob = _prob_hit(good, deck_size, draws)
            if prob > best_prob:
                best_prob = prob
                best = (keep, prob)

    if best and best_prob > 0:
        return best
    return None


def flush_five_draw_quality(
    hand_cards: list[dict], deck_cards: list[dict],
    smeared: bool = False, rank_affinity: dict[str, float] | None = None,
) -> tuple[list[int], float] | None:
    """Chase Flush Five (5 cards of same suit that share a rank).

    Requires 4+ suited cards of the same rank in hand + 1 more from deck
    matching both suit and rank.  Only realistic with Wild or Smeared Joker.
    """
    suits_to_indices: dict[str, list[int]] = {}
    for i, c in enumerate(hand_cards):
        for s in card_suits(c, smeared=smeared):
            suits_to_indices.setdefault(s, [])
            if i not in suits_to_indices[s]:
                suits_to_indices[s].append(i)

    deck_size = len(deck_cards)
    best: tuple[list[int], float] | None = None
    best_prob = -1.0

    for suit, indices in suits_to_indices.items():
        if len(indices) < 4:
            continue

        # Count ranks within this suit
        suit_rank_indices: dict[str, list[int]] = {}
        for idx in indices:
            r = card_rank(hand_cards[idx])
            if r:
                suit_rank_indices.setdefault(r, []).append(idx)

        # Need 4 of the same rank in this suit — prefer high affinity/rank
        candidates = sorted(
            [(r, idxs) for r, idxs in suit_rank_indices.items() if len(idxs) >= 4],
            key=lambda x: (
                rank_affinity.get(x[0], 0.0) if rank_affinity else 0.0,
                rank_value(x[0]),
            ),
            reverse=True,
        )
        for rank, rank_idxs in candidates:
            keep = rank_idxs[:4]
            draws = len(hand_cards) - len(keep)

            good = sum(
                1 for c in deck_cards
                if card_rank(c) == rank and suit in card_suits(c, smeared=smeared)
            )
            prob = _prob_hit(good, deck_size, draws)
            if prob > best_prob:
                best_prob = prob
                best = (keep, prob)

    if best and best_prob > 0:
        return best
    return None
