"""
Hand evaluator for Balatro.

Takes a list of Card dicts (from the balatrobot API) and enumerates all valid
poker hands that can be formed from any subset, ranked by effective Balatro
score (chips * mult after base hand values and card chip contributions).

Key Balatro scoring differences from standard poker:
- Flush Five, Flush House, and Five of a Kind exist as hand types.
- Stone cards have no suit/rank but contribute +50 chips.
- Wild cards count as every suit for flush purposes.
- Steel cards give ×1.5 mult while in hand (not scored here — that's joker-level).
- Scoring = (hand_base_chips + sum of card chips) × hand_base_mult.
"""

from __future__ import annotations

from itertools import combinations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RANK_ORDER: dict[str, int] = {
    "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8, "9": 9,
    "T": 10, "J": 11, "Q": 12, "K": 13, "A": 14,
}

# Chip value each rank contributes when it scores.
RANK_CHIPS: dict[str, int] = {
    "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8, "9": 9,
    "T": 10, "J": 10, "Q": 10, "K": 10, "A": 11,
}

ALL_SUITS = {"H", "D", "C", "S"}

# Balatro hand types ordered by priority (highest first).
# (name, base_chips, base_mult)
HAND_TYPES: list[tuple[str, int, int]] = [
    ("Flush Five",      160, 16),
    ("Flush House",     140, 14),
    ("Five of a Kind",  120, 12),
    ("Straight Flush",  100,  8),
    ("Four of a Kind",   60,  7),
    ("Full House",       40,  4),
    ("Flush",            35,  4),
    ("Straight",         30,  4),
    ("Three of a Kind",  30,  3),
    ("Two Pair",         20,  2),
    ("Pair",             10,  2),
    ("High Card",         5,  1),
]

# Quick lookup: name -> (base_chips, base_mult, priority_rank)
HAND_INFO: dict[str, tuple[int, int, int]] = {
    name: (chips, mult, i)
    for i, (name, chips, mult) in enumerate(HAND_TYPES)
}


# ---------------------------------------------------------------------------
# Card helpers
# ---------------------------------------------------------------------------

def _modifier(card: dict[str, Any]) -> dict[str, Any]:
    """Return the modifier dict, handling the API returning [] for empty."""
    m = card.get("modifier", {})
    return m if isinstance(m, dict) else {}


def _state(card: dict[str, Any]) -> dict[str, Any]:
    """Return the state dict, handling the API returning [] for empty."""
    s = card.get("state", {})
    return s if isinstance(s, dict) else {}


def is_debuffed(card: dict[str, Any]) -> bool:
    return _state(card).get("debuff", False) is True


def card_rank(card: dict[str, Any]) -> str | None:
    """Return the rank character, or None for Stone/non-playing cards."""
    return card.get("value", {}).get("rank")


def card_suit(card: dict[str, Any]) -> str | None:
    """Return the suit character, or None for Stone/non-playing cards."""
    return card.get("value", {}).get("suit")


def card_suits(card: dict[str, Any]) -> set[str]:
    """Return all suits this card counts as (Wild = all four)."""
    enhancement = _modifier(card).get("enhancement")
    if enhancement == "WILD":
        return set(ALL_SUITS)
    suit = card_suit(card)
    return {suit} if suit else set()


def is_stone(card: dict[str, Any]) -> bool:
    return _modifier(card).get("enhancement") == "STONE"


def card_chip_value(card: dict[str, Any]) -> int:
    """Chips this card contributes when it scores in a played hand."""
    if is_debuffed(card):
        return 0
    if is_stone(card):
        return 50
    mod = _modifier(card)
    enhancement = mod.get("enhancement", "")
    edition = mod.get("edition", "")
    bonus = 30 if enhancement == "BONUS" else 0
    foil  = 50 if edition == "FOIL" else 0
    rank = card_rank(card)
    base = RANK_CHIPS.get(rank, 0) if rank else 0
    # Permanent chip bonus from Hiker etc. (exposed by API as value.perma_bonus)
    perma = card.get("value", {}).get("perma_bonus", 0) or 0
    return base + bonus + foil + perma


def card_mult_value(card: dict[str, Any]) -> int:
    """Additive mult this card contributes when it scores in a played hand.

    Covers card enhancements and editions that add flat mult:
      MULT enhancement  → +4 Mult
      Holographic edition → +10 Mult
      LUCKY enhancement → +1 Mult (expected: 1/15 × 20 ≈ 1.3)
    """
    if is_debuffed(card):
        return 0
    if is_stone(card):
        return 0
    mod = _modifier(card)
    enhancement = mod.get("enhancement", "")
    edition = mod.get("edition", "")
    total = 0
    if enhancement == "MULT":
        total += 4
    if edition in ("HOLO", "HOLOGRAPHIC"):
        total += 10
    if enhancement == "LUCKY":
        total += 4  # expected value: 1/5 chance × +20 Mult = +4
    return total


def card_xmult_value(card: dict[str, Any]) -> float:
    """Multiplicative xmult this card contributes when it scores in a played hand.

    Covers card enhancements and editions that multiply final score:
      GLASS enhancement  → ×2.0 (shatter risk only affects future hands)
      Polychrome edition → ×1.5
    """
    if is_debuffed(card):
        return 1.0
    if is_stone(card):
        return 1.0
    mod = _modifier(card)
    enhancement = mod.get("enhancement", "")
    edition = mod.get("edition", "")
    result = 1.0
    if enhancement == "GLASS":
        result *= 2.0
    if edition == "POLYCHROME":
        result *= 1.5
    return result


def rank_value(rank: str) -> int:
    return RANK_ORDER.get(rank, 0)


# ---------------------------------------------------------------------------
# Hand classification
# ---------------------------------------------------------------------------

def _rank_counts(cards: list[dict]) -> dict[str, int]:
    """Count occurrences of each rank (ignoring Stone cards)."""
    counts: dict[str, int] = {}
    for c in cards:
        r = card_rank(c)
        if r is not None:
            counts[r] = counts.get(r, 0) + 1
    return counts


def _is_flush(cards: list[dict]) -> bool:
    """True if all cards share at least one common suit."""
    if not cards:
        return False
    common = card_suits(cards[0])
    for c in cards[1:]:
        common &= card_suits(c)
        if not common:
            return False
    return bool(common)


def _is_straight(cards: list[dict], four_fingers: bool = False) -> bool:
    """True if the ranked cards form a straight (A-low allowed).

    Normally requires 5 consecutive ranks. With four_fingers=True (j_four_fingers),
    4 consecutive ranks suffice.
    """
    ranks = sorted({rank_value(card_rank(c)) for c in cards if card_rank(c)})
    min_len = 4 if four_fingers else 5
    if len(ranks) < min_len:
        return False
    # Consecutive check: range == count - 1 works for any length
    if ranks[-1] - ranks[0] == len(ranks) - 1:
        return True
    # Ace-low: A plays as 1. Remove Ace, check remaining are min_len-1
    # consecutive ranks starting at 2. Covers A-2-3-4-5 and A-2-3-4.
    if 14 in ranks:
        low = [r for r in ranks if r != 14]
        if (len(low) >= min_len - 1
                and low[0] == 2
                and low[-1] - low[0] == len(low) - 1):
            return True
    return False


def classify_hand(cards: list[dict], four_fingers: bool = False) -> str:
    """Return the best Balatro hand name for a set of cards.

    four_fingers: when True (j_four_fingers owned), flushes and straights
    can be formed with 4 cards instead of 5.
    """
    n = len(cards)
    if n == 0:
        return "High Card"

    rc = _rank_counts(cards)
    counts_sorted = sorted(rc.values(), reverse=True)
    min_sf = 4 if four_fingers else 5
    flush    = _is_flush(cards) and n >= min_sf
    straight = _is_straight(cards, four_fingers) and n >= min_sf

    max_kind = counts_sorted[0] if counts_sorted else 0

    # Five of a Kind family
    if max_kind >= 5:
        if flush:
            return "Flush Five"
        return "Five of a Kind"

    # Four of a Kind
    if max_kind == 4:
        return "Four of a Kind"

    # Full House family (3+2)
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

def flush_draw(hand_cards):
    """If 4+ cards share a suit, return their indices."""
    suits_to_indices: dict[str, list[int]] = {}
    for i, card in enumerate(hand_cards):
        for suit in card_suits(card):
            if suit not in suits_to_indices:
                suits_to_indices[suit] = []
            if i not in suits_to_indices[suit]:
                suits_to_indices[suit].append(i)

    for suit, indices in suits_to_indices.items():
        if len(indices) >=4:
            return indices[:4]

    return None

def straight_draw(hand_cards):
    """If 4 cards are in sequence, return their indices."""
    rank_to_indices: dict[str, list[int]] = {}
    for i, card in enumerate(hand_cards):
        rank  = card_rank(card)
        if rank is None:
            continue
        if rank not in rank_to_indices:
           rank_to_indices[rank] = []
        rank_to_indices[rank].append(i)

    rank_values = sorted(rank_to_indices.keys(), key=rank_value)

    for i in range(len(rank_values) - 3):
        window = rank_values[i:i+4]
        lo = rank_value(window[0])
        hi = rank_value(window[-1])
        if hi - lo == 3:  # exactly 4 consecutive ranks
            indices = []
            for r in window:
                indices.append(rank_to_indices[r][0])
            return indices

    # Ace-low: check for A-2-3-4 (rank values {14, 2, 3, 4})
    rank_val_set = {rank_value(r) for r in rank_to_indices}
    if {14, 2, 3, 4} <= rank_val_set:
        # Map rank values back to rank chars
        val_to_rank = {rank_value(r): r for r in rank_to_indices}
        indices = [rank_to_indices[val_to_rank[v]][0] for v in (14, 2, 3, 4)]
        return indices

    return None



# ---------------------------------------------------------------------------
# Draw probability helpers
# ---------------------------------------------------------------------------

def _prob_two_or_more(good: int, deck_size: int, draws: int) -> float:
    """P(draw at least 2 of `good` cards in `draws` from deck of `deck_size`).

    Uses exact hypergeometric: 1 - P(0 hits) - P(exactly 1 hit).
    """
    from math import comb
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
    """P(draw at least 1 of `good` cards in `draws` from deck of `deck_size`).

    Uses exact hypergeometric: P(hit) = 1 - P(miss all).
    """
    if deck_size <= 0 or draws <= 0 or good <= 0:
        return 0.0
    if good >= deck_size:
        return 1.0
    # P(miss all) = product of (D-K-i)/(D-i) for i in 0..N-1
    p_miss = 1.0
    for i in range(min(draws, deck_size)):
        if deck_size - i <= 0:
            break
        p_miss *= (deck_size - good - i) / (deck_size - i)
        if p_miss <= 0:
            return 1.0
    return max(0.0, min(1.0, 1.0 - p_miss))


def flush_draw_quality(
    hand_cards: list[dict], deck_cards: list[dict],
) -> tuple[list[int], float, str] | None:
    """If 4+ cards share a suit, return (keep_indices, probability, suit).

    Probability = chance of drawing at least 1 more of that suit.
    """
    suits_to_indices: dict[str, list[int]] = {}
    for i, card in enumerate(hand_cards):
        for suit in card_suits(card):
            if suit not in suits_to_indices:
                suits_to_indices[suit] = []
            if i not in suits_to_indices[suit]:
                suits_to_indices[suit].append(i)

    best: tuple[list[int], float, str] | None = None
    best_prob = -1.0

    for suit, indices in suits_to_indices.items():
        if len(indices) < 4:
            continue
        keep = indices[:4]
        cards_to_draw = len(hand_cards) - len(keep)
        # Count how many of this suit are in the remaining deck
        suit_in_deck = sum(1 for c in deck_cards if suit in card_suits(c))
        deck_size = len(deck_cards)
        prob = _prob_hit(suit_in_deck, deck_size, cards_to_draw)
        if prob > best_prob:
            best_prob = prob
            best = (keep, prob, suit)

    return best


def straight_draw_quality(
    hand_cards: list[dict], deck_cards: list[dict],
) -> tuple[list[int], float] | None:
    """If 4 cards are in sequence, return (keep_indices, probability).

    Probability = chance of drawing at least 1 card that completes the straight.
    """
    rank_to_indices: dict[str, list[int]] = {}
    for i, card in enumerate(hand_cards):
        rank = card_rank(card)
        if rank is None:
            continue
        if rank not in rank_to_indices:
            rank_to_indices[rank] = []
        rank_to_indices[rank].append(i)

    rank_values_list = sorted(rank_to_indices.keys(), key=rank_value)

    best: tuple[list[int], float] | None = None
    best_prob = -1.0

    # Collect candidate windows: normal consecutive + Ace-low (A-2-3-4)
    windows: list[list[str]] = []
    for i in range(len(rank_values_list) - 3):
        window = rank_values_list[i:i + 4]
        lo = rank_value(window[0])
        hi = rank_value(window[-1])
        if hi - lo == 3:
            windows.append(window)

    # Ace-low: A-2-3-4 (rank values 14,2,3,4 — not caught by hi-lo==3)
    rank_val_set = {rank_value(r) for r in rank_to_indices}
    if {14, 2, 3, 4} <= rank_val_set:
        val_to_rank = {rank_value(r): r for r in rank_to_indices}
        windows.append([val_to_rank[v] for v in (14, 2, 3, 4)])

    for window in windows:
        lo = rank_value(window[0])
        hi = rank_value(window[-1])

        keep = [rank_to_indices[r][0] for r in window]
        cards_to_draw = len(hand_cards) - len(keep)

        # Need cards to complete the straight — could be low end, high end, or both
        needed_ranks: set[int] = set()

        # Ace-low window (A-2-3-4): need a 5
        if {rank_value(r) for r in window} == {14, 2, 3, 4}:
            needed_ranks.add(5)
        else:
            if lo > 2:
                needed_ranks.add(lo - 1)  # card below
            if hi < 14:
                needed_ranks.add(hi + 1)  # card above
            # Special: if window is 2-3-4-5, Ace (14) also works for A-low straight
            if lo == 2 and hi == 5:
                needed_ranks.add(14)
            # Special: if window is T-J-Q-K, Ace works for A-high straight
            if lo == 10 and hi == 13:
                needed_ranks.add(14)

        # Count matching cards in deck
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
) -> tuple[list[int], float] | None:
    """If hand has a pair, return (keep_indices, probability) for chasing Two Pair.

    Keeps the best pair (2 cards), draws the rest. Probability = chance that at
    least one of the drawn cards pairs with another drawn card (any rank besides
    the kept pair's rank).
    """
    rank_to_indices: dict[str, list[int]] = {}
    for i, c in enumerate(hand_cards):
        r = card_rank(c)
        if r:
            rank_to_indices.setdefault(r, []).append(i)

    pairs = [(r, idxs) for r, idxs in rank_to_indices.items() if len(idxs) >= 2]
    if not pairs:
        return None

    # Keep the highest-rank pair
    pair_rank, pair_indices = max(pairs, key=lambda x: rank_value(x[0]))
    keep = pair_indices[:2]
    draws = len(hand_cards) - len(keep)

    # Count deck copies per rank (excluding the kept pair's rank)
    deck_rank_counts: dict[str, int] = {}
    for c in deck_cards:
        r = card_rank(c)
        if r and r != pair_rank:
            deck_rank_counts[r] = deck_rank_counts.get(r, 0) + 1

    deck_size = len(deck_cards)
    # P(new pair forms among draws) ≈ sum over ranks of P(2+ of that rank drawn)
    # Events are ~mutually exclusive (hitting 2 ranks is rare in 3 draws)
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
) -> tuple[list[int], float] | None:
    """If hand has a pair, return (keep_indices, probability) for chasing Three of a Kind.

    Keeps the pair with the most copies remaining in deck (maximizes trip odds),
    draws the rest. Probability = chance of drawing at least 1 more of that rank.
    """
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

    # Pick pair with most outs (most copies of that rank left in deck)
    pair_rank, pair_indices = max(pairs, key=lambda x: deck_rank_counts.get(x[0], 0))
    keep = pair_indices[:2]
    draws = len(hand_cards) - len(keep)

    good = deck_rank_counts.get(pair_rank, 0)
    deck_size = len(deck_cards)
    prob = _prob_hit(good, deck_size, draws)

    if prob <= 0:
        return None
    return (keep, prob)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score_hand(
    hand_name: str,
    scoring_cards: list[dict],
    hand_levels: dict[str, dict] | None = None,
    jokers: list[dict] | None = None,
    played_cards: list[dict] | None = None,
    held_cards: list[dict] | None = None,
    money: int = 0,
    discards_left: int = 0,
    hands_left: int = 1,
    joker_limit: int = 5,
    ancient_suit: str | None = None,
) -> tuple[int, int, int]:
    """
    Compute (chips, mult, total) for a hand.

    Without jokers, computes base score only (backwards compatible).
    With jokers, applies the full joker effect pipeline including retriggers.
    """
    base_chips, base_mult, _ = HAND_INFO[hand_name]

    if hand_levels and hand_name in hand_levels:
        lvl = hand_levels[hand_name]
        base_chips = lvl.get("chips", base_chips)
        base_mult = lvl.get("mult", base_mult)

    if not jokers:
        # Fast path: no jokers — still apply card enhancements/editions.
        # Process cards in order: chips/mult additive, then xmult folds into running mult.
        total_chips = base_chips
        total_mult  = float(base_mult)
        for c in scoring_cards:
            total_chips += card_chip_value(c)
            total_mult  += card_mult_value(c)
            xmv = card_xmult_value(c)
            if xmv != 1.0:
                total_mult *= xmv
        # Steel held cards multiply running mult
        for c in (held_cards or []):
            if not is_debuffed(c) and _modifier(c).get("enhancement") == "STEEL":
                total_mult *= 1.5
        total = round(total_chips * total_mult)
        return total_chips, total_mult, total

    # Full scoring pipeline with joker effects and retriggers
    from joker_effects import ScoreContext, apply_joker_effects, retrigger_count

    joker_keys_set = {j.get("key") for j in jokers}
    ctx = ScoreContext(
        chips=base_chips,
        mult=float(base_mult),
        hand_name=hand_name,
        scoring_cards=scoring_cards,
        played_cards=played_cards or scoring_cards,
        held_cards=held_cards or [],
        hand_levels=hand_levels or {},
        jokers=jokers,
        money=money,
        discards_left=discards_left,
        hands_left=hands_left,
        joker_limit=joker_limit,
        pareidolia="j_pareidolia" in joker_keys_set,
        ancient_suit=ancient_suit,
    )

    # Card scoring with retrigger support.
    # Each scored card contributes chips, additive mult, then xmult folds
    # into running mult at the point it fires (matching Balatro's pipeline).
    for card in scoring_cards:
        triggers = retrigger_count(card, ctx)
        ctx.chips += card_chip_value(card) * triggers
        ctx.mult  += card_mult_value(card) * triggers
        xmv = card_xmult_value(card)
        if xmv != 1.0:
            for _ in range(triggers):
                ctx.mult *= xmv

    # Steel cards held in hand give ×1.5 mult while not played.
    for card in ctx.held_cards:
        if not is_debuffed(card) and _modifier(card).get("enhancement") == "STEEL":
            ctx.mult *= 1.5

    pre_joker_chips = ctx.chips
    pre_joker_mult = ctx.mult

    # Apply joker effects in order
    apply_joker_effects(ctx)

    total = round(ctx.chips * ctx.mult)

    # Log suspicious scores to dedicated file for diagnosis
    if jokers and total < 200 and hand_name in ("Full House", "Flush", "Straight", "Four of a Kind"):
        import logging
        _score_log = logging.getLogger("score_debug")
        if not _score_log.handlers:
            _fh = logging.FileHandler("score_debug.txt", mode="a", encoding="utf-8")
            _fh.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S"))
            _score_log.addHandler(_fh)
            _score_log.setLevel(logging.DEBUG)
        joker_details = [
            (j.get("key", "?"), j.get("value", {}).get("effect", ""))
            for j in jokers
        ]
        _score_log.debug(
            "SUSPICIOUS %s: base=%d/%d card_chips=%d pre_joker=%d/%.1f "
            "post_joker=%d/%.1f total=%d joker_effects=%s hand_lvl=%s",
            hand_name, base_chips, base_mult,
            pre_joker_chips - base_chips, pre_joker_chips, pre_joker_mult,
            ctx.chips, ctx.mult, total,
            joker_details,
            hand_levels.get(hand_name) if hand_levels else None,
        )

    return ctx.chips, ctx.mult, total


def score_hand_detailed(
    hand_name: str,
    scoring_cards: list[dict],
    hand_levels: dict[str, dict] | None = None,
    jokers: list[dict] | None = None,
    played_cards: list[dict] | None = None,
    held_cards: list[dict] | None = None,
    money: int = 0,
    discards_left: int = 0,
    hands_left: int = 1,
    joker_limit: int = 5,
    ancient_suit: str | None = None,
) -> dict:
    """Like score_hand but returns a full breakdown dict for logging.

    Only call for hands actually played — never in enumerate_hands.
    """
    from joker_effects import ScoreContext, apply_joker_effects_detailed, retrigger_count

    base_chips, base_mult, _ = HAND_INFO[hand_name]
    if hand_levels and hand_name in hand_levels:
        lvl = hand_levels[hand_name]
        base_chips = lvl.get("chips", base_chips)
        base_mult = lvl.get("mult", base_mult)

    # Per-card contributions
    card_details = []
    for c in scoring_cards:
        label = c.get("label", "?")
        chips = card_chip_value(c)
        mult = card_mult_value(c)
        xmult = card_xmult_value(c)
        mod = c.get("modifier", {})
        if isinstance(mod, dict) and (mod.get("edition") or mod.get("enhancement")):
            label += f"[{mod.get('edition','')}/{mod.get('enhancement','')}]"
        card_details.append((label, chips, mult, xmult))

    if not jokers:
        total_chips = base_chips
        total_mult = float(base_mult)
        for _, chips, mult, xmult in card_details:
            total_chips += chips
            total_mult += mult
            if xmult != 1.0:
                total_mult *= xmult
        for c in (held_cards or []):
            if not is_debuffed(c) and _modifier(c).get("enhancement") == "STEEL":
                total_mult *= 1.5
        return {
            "hand_name": hand_name,
            "base_chips": base_chips, "base_mult": base_mult,
            "card_details": card_details,
            "pre_joker_chips": total_chips, "pre_joker_mult": total_mult,
            "joker_contributions": [],
            "post_joker_chips": total_chips, "post_joker_mult": total_mult,
            "total": round(total_chips * total_mult),
        }

    joker_keys_set = {j.get("key") for j in jokers}
    ctx = ScoreContext(
        chips=base_chips,
        mult=float(base_mult),
        hand_name=hand_name,
        scoring_cards=scoring_cards,
        played_cards=played_cards or scoring_cards,
        held_cards=held_cards or [],
        hand_levels=hand_levels or {},
        jokers=jokers,
        money=money,
        discards_left=discards_left,
        hands_left=hands_left,
        joker_limit=joker_limit,
        pareidolia="j_pareidolia" in joker_keys_set,
        ancient_suit=ancient_suit,
    )

    for card in scoring_cards:
        triggers = retrigger_count(card, ctx)
        ctx.chips += card_chip_value(card) * triggers
        ctx.mult += card_mult_value(card) * triggers
        xmv = card_xmult_value(card)
        if xmv != 1.0:
            for _ in range(triggers):
                ctx.mult *= xmv

    for card in ctx.held_cards:
        if not is_debuffed(card) and _modifier(card).get("enhancement") == "STEEL":
            ctx.mult *= 1.5

    pre_joker_chips = ctx.chips
    pre_joker_mult = ctx.mult

    joker_contributions = apply_joker_effects_detailed(ctx)

    total = round(ctx.chips * ctx.mult)
    return {
        "hand_name": hand_name,
        "base_chips": base_chips, "base_mult": base_mult,
        "card_details": card_details,
        "pre_joker_chips": pre_joker_chips, "pre_joker_mult": pre_joker_mult,
        "joker_contributions": joker_contributions,
        "post_joker_chips": ctx.chips, "post_joker_mult": ctx.mult,
        "total": total,
    }


# ---------------------------------------------------------------------------
# Scoring cards extraction
# ---------------------------------------------------------------------------

def _scoring_cards_for(hand_name: str, cards: list[dict]) -> list[dict]:
    """
    Return the subset of *cards* that actually score for *hand_name*.

    In Balatro, only the cards that form the hand type score (contribute chips).
    E.g. in a Pair of Kings from [K, K, 7, 3, 2], only the two Kings score.
    """
    rc = _rank_counts(cards)
    counts_sorted = sorted(rc.items(), key=lambda x: (-x[1], -rank_value(x[0])))

    if hand_name in ("Flush", "Straight", "Straight Flush", "Flush Five", "Flush House"):
        return list(cards)  # all cards in the combo score

    if hand_name == "Five of a Kind":
        target_rank = counts_sorted[0][0]
        return [c for c in cards if card_rank(c) == target_rank][:5]

    if hand_name == "Four of a Kind":
        target_rank = next(r for r, cnt in counts_sorted if cnt >= 4)
        return [c for c in cards if card_rank(c) == target_rank][:4]

    if hand_name == "Full House":
        trip_rank = next(r for r, cnt in counts_sorted if cnt >= 3)
        pair_rank = next(r for r, cnt in counts_sorted if cnt >= 2 and r != trip_rank)
        trips = [c for c in cards if card_rank(c) == trip_rank][:3]
        pairs = [c for c in cards if card_rank(c) == pair_rank][:2]
        return trips + pairs

    if hand_name == "Three of a Kind":
        target_rank = next(r for r, cnt in counts_sorted if cnt >= 3)
        return [c for c in cards if card_rank(c) == target_rank][:3]

    if hand_name == "Two Pair":
        pair_ranks = [r for r, cnt in counts_sorted if cnt >= 2][:2]
        result = []
        for pr in pair_ranks:
            result.extend(c for c in cards if card_rank(c) == pr)
        return result[:4]

    if hand_name == "Pair":
        target_rank = next(r for r, cnt in counts_sorted if cnt >= 2)
        return [c for c in cards if card_rank(c) == target_rank][:2]

    # High Card — highest single card scores
    ranked = [c for c in cards if card_rank(c)]
    ranked.sort(key=lambda c: rank_value(card_rank(c)), reverse=True)  # type: ignore[arg-type]
    return ranked[:1] if ranked else cards[:1]


# ---------------------------------------------------------------------------
# Public API: enumerate all playable hands
# ---------------------------------------------------------------------------

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
        self.priority = HAND_INFO[hand_name][2]  # lower = better

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
) -> list[HandCandidate]:
    """
    Enumerate all valid poker hands from the cards in hand.

    Returns a list of HandCandidate sorted by:
      1. Total score descending (joker-aware if jokers provided)
      2. Fewer cards preferred (leave more in hand)

    Parameters
    ----------
    hand_cards : list of card dicts from state["hand"]["cards"]
    hand_levels : state["hands"] dict for leveled scoring (optional)
    max_select : max cards you can select (usually 5)
    min_select : min cards to play (usually 1)
    jokers : owned joker card dicts for joker-aware scoring (optional)
    money : current dollars (for Bull, Bootstraps, etc.)
    discards_left : remaining discards (for Banner, Mystic Summit, etc.)
    hands_left : remaining hands (for Acrobat, Dusk, etc.)
    required_card_indices : if set, only combos that include all of these indices
        are considered (Cerulean Bell forces one card into every play)
    """
    candidates: list[HandCandidate] = []
    n = len(hand_cards)
    indices_set = set(range(n))

    joker_keys = {j.get("key") for j in (jokers or [])}
    four_fingers = "j_four_fingers" in joker_keys
    has_splash   = "j_splash" in joker_keys

    for size in range(min_select, min(max_select, n) + 1):
        for indices in combinations(range(n), size):
            # Cerulean Bell: skip combos that don't include the forced card
            if required_card_indices and not required_card_indices.issubset(set(indices)):
                continue
            subset = [hand_cards[i] for i in indices]
            hand_name = classify_hand(subset, four_fingers=four_fingers)

            # Splash: every played card scores, not just the hand-type subset
            scoring = subset if has_splash else _scoring_cards_for(hand_name, subset)
            held = [hand_cards[i] for i in indices_set - set(indices)] if jokers else []
            chips, mult, total = score_hand(
                hand_name, scoring, hand_levels,
                jokers=jokers, played_cards=subset, held_cards=held,
                money=money, discards_left=discards_left, hands_left=hands_left,
                joker_limit=joker_limit, ancient_suit=ancient_suit,
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

    # The Mouth: only one hand type allowed after the first play this round
    if required_hand:
        candidates = [c for c in candidates if c.hand_name == required_hand]

    # With jokers, total score is king — a joker-boosted Pair can beat a Flush.
    # Without jokers, break ties by hand type priority as a tiebreaker.
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
    )
    return candidates[0] if candidates else None


# ---------------------------------------------------------------------------
# Discard analysis
# ---------------------------------------------------------------------------

def cards_not_in(
    hand_cards: list[dict], keep_indices: set[int], blackboard: bool = False
) -> list[int]:
    """Return indices of cards NOT in the keep set — candidates for discard.

    Debuffed cards are returned first (they contribute nothing, best to discard).
    When blackboard=True, H/D cards sort before S/C so we preferentially discard
    off-suit cards and preserve Spades/Clubs for Blackboard's ×3 mult trigger.
    """
    candidates = [i for i in range(len(hand_cards)) if i not in keep_indices]
    candidates.sort(key=lambda i: (
        0 if is_debuffed(hand_cards[i]) else 1,
        # Blackboard bias: H/D (not S/C) are preferred discard targets
        0 if blackboard and card_suit(hand_cards[i]) in ("H", "D") else 1,
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
) -> list[tuple[list[int], str]]:
    """
    Suggest discard sets that improve toward better hands.

    Returns list of (discard_indices, reason) sorted by expected value
    (hand score × draw probability).

    strategy_affinity: optional {hand_name: affinity_score} from Strategy.
    deck_cards: cards remaining in the draw pile for probability calculation.
    chips_remaining: chips still needed to beat the blind (for hopeless detection).
    jokers: owned joker dicts for joker-aware scoring (matches rule engine's evaluation).
    """
    best = best_hand(hand_cards, hand_levels, max_select, jokers=jokers, required_hand=required_hand)
    if not best:
        return [(list(range(min(max_discard, len(hand_cards)))), "no hand found")]

    # Build strategies: (chase_hand_name, keep_indices, probability, reason)
    strategies: list[tuple[str, list[int], float, str]] = []

    # Strategy 0: hopeless redraw — when the hand covers < 10% of what's
    # needed, discard everything EXCEPT the best hand's cards. This preserves
    # the existing Pair/Two Pair/etc. while replacing the dead weight.
    if chips_remaining > 0 and best.total < chips_remaining * 0.10:
        keep_indices = best.card_indices
        n_discard = min(max_discard, len(hand_cards) - len(keep_indices))
        if n_discard > 0:
            strategies.append((
                "redraw",
                keep_indices,
                0.5,
                f"redraw {n_discard} cards ({best.hand_name} for {best.total} is hopeless vs {chips_remaining} needed)",
            ))

    # Strategy 1: keep current best hand (100% — we already have it)
    strategies.append((
        best.hand_name,
        best.card_indices,
        1.0,
        f"keep {best.hand_name}, discard dead cards",
    ))

    # Strategy 2: chase a flush (only if flush is better than current best)
    if HAND_INFO["Flush"][2] < HAND_INFO[best.hand_name][2]:
        if deck_cards:
            fdq = flush_draw_quality(hand_cards, deck_cards)
            if fdq:
                indices, prob, suit = fdq
                strategies.append((
                    "Flush",
                    indices,
                    prob,
                    f"chase Flush ({prob:.0%} to hit, {suit}), discard {len(hand_cards) - len(indices)} cards",
                ))
        else:
            # No deck info — fall back to old behavior
            flush_indices = flush_draw(hand_cards)
            if flush_indices:
                strategies.append((
                    "Flush",
                    flush_indices,
                    0.5,  # assume 50% without deck data
                    f"chase Flush, discard {len(hand_cards) - len(flush_indices)} cards",
                ))

    # Strategy 3: chase a straight (only if straight is better than current best)
    if HAND_INFO["Straight"][2] < HAND_INFO[best.hand_name][2]:
        if deck_cards:
            sdq = straight_draw_quality(hand_cards, deck_cards)
            if sdq:
                indices, prob = sdq
                strategies.append((
                    "Straight",
                    indices,
                    prob,
                    f"chase Straight ({prob:.0%} to hit), discard {len(hand_cards) - len(indices)} cards",
                ))
        else:
            straight_indices = straight_draw(hand_cards)
            if straight_indices:
                strategies.append((
                    "Straight",
                    straight_indices,
                    0.5,
                    f"chase Straight, discard {len(hand_cards) - len(straight_indices)} cards",
                ))

    # Strategy 4: chase Two Pair (only if Two Pair is better than current best
    # and we have deck data — no fallback needed, low-info guess isn't useful)
    if deck_cards and HAND_INFO["Two Pair"][2] < HAND_INFO[best.hand_name][2]:
        tpdq = two_pair_draw_quality(hand_cards, deck_cards)
        if tpdq:
            indices, prob = tpdq
            strategies.append((
                "Two Pair",
                indices,
                prob,
                f"chase Two Pair ({prob:.0%} to hit), discard {len(hand_cards) - len(indices)} cards",
            ))

    # Strategy 5: chase Three of a Kind (only if better than current best)
    if deck_cards and HAND_INFO["Three of a Kind"][2] < HAND_INFO[best.hand_name][2]:
        tkdq = three_kind_draw_quality(hand_cards, deck_cards)
        if tkdq:
            indices, prob = tkdq
            strategies.append((
                "Three of a Kind",
                indices,
                prob,
                f"chase Three of a Kind ({prob:.0%} to hit), discard {len(hand_cards) - len(indices)} cards",
            ))

    # The Mouth: strip any strategy that chases a different hand type
    if required_hand:
        strategies = [
            (n, k, p, r) for n, k, p, r in strategies
            if n == required_hand or n == "redraw"
        ]

    # Sort by expected value, heavily weighted by strategy.
    # If we have a strategy, hands the strategy favors get a massive boost,
    # and hands it doesn't favor get penalized. This prevents chasing a
    # Flush when all our jokers boost Pairs.
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
                # Strategy wants this hand — big boost
                base *= (1.0 + affinity * 0.5)
            elif hand_name != best.hand_name:
                # Strategy doesn't want this hand and it's a chase (not "keep")
                # Penalize heavily — don't chase off-strategy hands
                base *= 0.3
        return base

    strategies.sort(key=chase_score, reverse=True)

    # Convert to (discard_indices, reason), filtering out anything over max_discard
    results = []
    has_blackboard = any(j.get("key") == "j_blackboard" for j in (jokers or []))

    for chase_name, keep, _prob, reason in strategies:
        to_discard = cards_not_in(hand_cards, set(keep), blackboard=has_blackboard)[:max_discard]
        if to_discard:
            results.append((to_discard, reason))

    return results
