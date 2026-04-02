"""
Hand evaluator for Balatro.

Takes a list of Card dicts (from the balatrobot API) and enumerates all valid
poker hands that can be formed from any subset, ranked by effective Balatro
score (chips * mult after base hand values and card chip contributions).
"""

from __future__ import annotations

import math
from itertools import combinations
from typing import TYPE_CHECKING, NamedTuple

from balatro_bot.constants import HAND_INFO, HAND_TYPES, RANK_ORDER
from balatro_bot.cards import (
    _modifier,
    card_chip_value,
    card_edition_mult_value,
    card_edition_xmult_value,
    card_mult_value,
    card_rank,
    card_suit,
    card_suits,
    card_xmult_value,
    is_debuffed,
    is_stone,
    rank_value,
)

if TYPE_CHECKING:
    from typing import Any


class ChaseCandidate(NamedTuple):
    """Enriched discard suggestion with chase metadata for EV calculation."""
    discard_indices: list[int]
    reason: str
    chase_hand: str        # hand type being chased (e.g. "Flush"), or current best
    keep_indices: list[int]
    hit_prob: float


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


def _is_flush(cards: list[dict], smeared: bool = False) -> bool:
    """True if all cards share at least one common suit."""
    if not cards:
        return False
    common = card_suits(cards[0], smeared=smeared)
    for c in cards[1:]:
        common &= card_suits(c, smeared=smeared)
        if not common:
            return False
    return bool(common)


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
    # Check if ranks form a run (each gap <= max_gap)
    if all(ranks[i + 1] - ranks[i] <= max_gap for i in range(len(ranks) - 1)):
        return True
    # Ace-low: treat Ace as 1
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
    flush    = _is_flush(cards, smeared=smeared) and n >= min_sf
    straight = _is_straight(cards, four_fingers, shortcut=shortcut) and n >= min_sf

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
            # Prefer keeping cards with high rank affinity
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
        # Shortcut ace-low: A + 3 low cards with gaps ≤ 2
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


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _apply_before_phase(scoring_cards, played_cards, jokers):
    """Simulate the game's 'before' phase — jokers that fire before card scoring.

    Vampire: strips enhancement from enhanced scoring cards, gains xmult.
    Returns (modified_scoring_cards, modified_played_cards, vampire_xmult).
    The played_cards list is updated so that stripped scoring cards replace
    their originals (needed for id()-based matching in _apply_card_scoring).
    """
    vampire = None
    for j in (jokers or []):
        if j.get("key") == "j_vampire":
            vampire = j
            break
    if not vampire:
        return scoring_cards, played_cards, None

    from balatro_bot.cards import is_debuffed, _modifier
    from balatro_bot.joker_effects.parsers import _ability, _ab_xmult

    ab = _ability(vampire)
    extra = ab.get("extra", 0.1)
    current_xmult = _ab_xmult(vampire, fallback=1.0)

    # Count enhanced scoring cards and build a mapping from original → stripped
    enhanced_count = 0
    orig_to_stripped = {}  # id(original) → stripped copy
    stripped_scoring = []
    for card in scoring_cards:
        mod = _modifier(card)
        enhancement = mod.get("enhancement", "")
        if enhancement and enhancement != "BASE" and not is_debuffed(card):
            enhanced_count += 1
            card_copy = dict(card)
            mod_copy = dict(mod)
            mod_copy.pop("enhancement", None)
            card_copy["modifier"] = mod_copy
            orig_to_stripped[id(card)] = card_copy
            stripped_scoring.append(card_copy)
        else:
            stripped_scoring.append(card)

    # Replace stripped cards in played_cards so id()-matching works
    stripped_played = played_cards
    if enhanced_count > 0 and played_cards is not None:
        stripped_played = [
            orig_to_stripped.get(id(c), c) for c in played_cards
        ]

    vampire_xmult = current_xmult + extra * enhanced_count if enhanced_count > 0 else current_xmult
    return stripped_scoring, stripped_played, vampire_xmult


def _apply_card_scoring(ctx, scoring_cards, played_cards, jokers, ancient_suit):
    """Score cards in played order with per-card joker effects interleaved.

    In Balatro, each card trigger replays the full per-card sequence:
    base chips → enhancement mult → Glass xmult → edition → per-card jokers.
    Per-card jokers ("when scored") fire WITHIN each trigger, not after.
    """
    from balatro_bot.joker_effects import retrigger_count
    from balatro_bot.constants import FACE_RANKS, FIBONACCI_RANKS, EVEN_RANKS, ODD_RANKS

    # Score in PLAYED order (matching the game), not internal scoring order
    scoring_id_set = set(id(c) for c in scoring_cards)
    scored_in_play_order = [c for c in (played_cards or scoring_cards) if id(c) in scoring_id_set]
    # Append any scoring cards not in played_cards (e.g., Stone cards)
    played_id_set = set(id(c) for c in scored_in_play_order)
    for c in scoring_cards:
        if id(c) not in played_id_set:
            scored_in_play_order.append(c)

    # Pre-scan jokers for per-card effects (fire per trigger, in joker order)
    # Each entry: (type, *params) applied per card per trigger
    _per_card = []
    _first_face_found = False  # shared state for Photograph
    for j in (jokers or []):
        k = j.get("key", "")
        ab = j.get("value", {}).get("ability", {})
        if k == "j_greedy_joker":
            _per_card.append(("suit_mult", "D", ab.get("s_mult", 3)))
        elif k == "j_lusty_joker":
            _per_card.append(("suit_mult", "H", ab.get("s_mult", 3)))
        elif k == "j_wrathful_joker":
            _per_card.append(("suit_mult", "S", ab.get("s_mult", 3)))
        elif k == "j_gluttenous_joker":
            _per_card.append(("suit_mult", "C", ab.get("s_mult", 3)))
        elif k == "j_fibonacci":
            _per_card.append(("ranks_mult", FIBONACCI_RANKS, ab.get("extra", 8)))
        elif k == "j_even_steven":
            _per_card.append(("ranks_mult", EVEN_RANKS, ab.get("extra", 4)))
        elif k == "j_odd_todd":
            _per_card.append(("ranks_chips", ODD_RANKS, ab.get("extra", 31)))
        elif k == "j_scholar":
            _per_card.append(("ranks_cm", frozenset({"A"}), ab.get("chips", 20), ab.get("mult", 4)))
        elif k == "j_smiley":
            _per_card.append(("face_mult", ab.get("extra", 5)))
        elif k == "j_scary_face":
            _per_card.append(("face_chips", ab.get("extra", 30)))
        elif k == "j_walkie_talkie":
            _per_card.append(("ranks_cm", frozenset({"T", "4"}), ab.get("chips", 10), ab.get("mult", 4)))
        elif k == "j_photograph":
            _per_card.append(("first_face_xmult", ab.get("extra", 2.0)))
        elif k == "j_triboulet":
            _per_card.append(("ranks_xmult", frozenset({"K", "Q"}), ab.get("extra", 2.0)))
        elif k == "j_ancient":
            _per_card.append(("suit_xmult", ancient_suit, 1.5))
        elif k == "j_arrowhead":
            _per_card.append(("suit_chips", "S", ab.get("extra", 50)))
        elif k == "j_onyx_agate":
            _per_card.append(("suit_mult", "C", ab.get("extra", 7)))
        elif k == "j_bloodstone":
            xm = ab.get("Xmult", 1.5)
            odds = ab.get("odds", 2)
            _per_card.append(("suit_expected_xmult", "H", xm, odds))

    for card in scored_in_play_order:
        triggers = retrigger_count(card, ctx)
        _is_first_face_card = False
        if not _first_face_found and not is_debuffed(card):
            rank_check = card_rank(card)
            if ctx.pareidolia or rank_check in FACE_RANKS:
                _is_first_face_card = True
                _first_face_found = True
        for _t in range(triggers):
            # 1. Base chips + enhancement chips + edition chips (Foil)
            ctx.chips += card_chip_value(card)
            # 2. Enhancement mult (MULT +4, Lucky +4 expected)
            ctx.mult += card_mult_value(card)
            # 3. Enhancement xmult (Glass x2.0)
            xmv = card_xmult_value(card)
            if xmv != 1.0:
                ctx.mult *= xmv
            # 4. Edition mult (HOLO +10)
            ed_mult = card_edition_mult_value(card)
            if ed_mult:
                ctx.mult += ed_mult
            # 5. Edition xmult (Polychrome x1.5)
            ed_xmult = card_edition_xmult_value(card)
            if ed_xmult != 1.0:
                ctx.mult *= ed_xmult
            # 6. Per-card joker effects (in joker order)
            if not is_debuffed(card):
                rank = card_rank(card)
                suits = card_suits(card, smeared=ctx.smeared) if _per_card else set()
                for eff in _per_card:
                    kind = eff[0]
                    if kind == "ranks_mult" and rank in eff[1]:
                        ctx.mult += eff[2]
                    elif kind == "ranks_chips" and rank in eff[1]:
                        ctx.chips += eff[2]
                    elif kind == "ranks_cm" and rank in eff[1]:
                        ctx.chips += eff[2]
                        ctx.mult += eff[3]
                    elif kind == "face_mult" and (ctx.pareidolia or rank in FACE_RANKS):
                        ctx.mult += eff[1]
                    elif kind == "face_chips" and (ctx.pareidolia or rank in FACE_RANKS):
                        ctx.chips += eff[1]
                    elif kind == "suit_mult" and eff[1] in suits:
                        ctx.mult += eff[2]
                    elif kind == "suit_chips" and eff[1] in suits:
                        ctx.chips += eff[2]
                    elif kind == "suit_xmult" and eff[1] and eff[1] in suits:
                        ctx.mult *= eff[2]
                    elif kind == "suit_expected_xmult" and eff[1] in suits:
                        # Bloodstone: expected value of probabilistic xmult
                        ctx.mult *= eff[2] ** (1.0 / eff[3])
                    elif kind == "ranks_xmult" and rank in eff[1]:
                        ctx.mult *= eff[2]
                    elif kind == "first_face_xmult":
                        # Photograph: x2 on every trigger of the FIRST face card,
                        # but not on subsequent face cards.
                        if _is_first_face_card:
                            ctx.mult *= eff[1]
            # 7. Gold Seal: +$3 per trigger (updates money for Bull mid-scoring)
            if not is_debuffed(card):
                seal = _modifier(card).get("seal", "")
                if seal == "GOLD":
                    ctx.money += 3

    # 8. The Tooth: -$1 per card played (all played cards, not just scoring)
    if ctx.blind_name == "The Tooth":
        ctx.money -= len(ctx.played_cards)

    # Held-in-hand effects: Steel, Baron, Shoot the Moon (fire before joker effects)
    # Mime retriggers all held card abilities (doubles each held card's effects)
    _baron_xm = 0.0
    _shoot_moon_mult = 0.0
    _has_mime = False
    for j in (jokers or []):
        k = j.get("key", "")
        if k == "j_baron":
            from balatro_bot.joker_effects.parsers import _ability as _ab
            _baron_xm = _ab(j).get("extra", 1.5)
        elif k == "j_shoot_the_moon":
            from balatro_bot.joker_effects.parsers import _ability as _ab
            _shoot_moon_mult = _ab(j).get("extra", 13)
        elif k == "j_mime":
            _has_mime = True

    _held_triggers = 2 if _has_mime else 1
    for card in ctx.held_cards:
        if not is_debuffed(card):
            for _ in range(_held_triggers):
                if _modifier(card).get("enhancement") == "STEEL":
                    ctx.mult *= 1.5
                if _baron_xm and card_rank(card) == "K":
                    ctx.mult *= _baron_xm
                if _shoot_moon_mult and card_rank(card) == "Q":
                    ctx.mult += _shoot_moon_mult


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
    deck_count: int = 0,
    deck_cards: list[dict] | None = None,
    blind_name: str = "",
) -> tuple[int, int, int]:
    """Compute (chips, mult, total) for a hand."""
    base_chips, base_mult, _ = HAND_INFO[hand_name]

    if hand_levels and hand_name in hand_levels:
        lvl = hand_levels[hand_name]
        base_chips = lvl.get("chips", base_chips)
        base_mult = lvl.get("mult", base_mult)

    if not jokers:
        total_chips = base_chips
        total_mult  = float(base_mult)
        for c in scoring_cards:
            total_chips += card_chip_value(c)
            total_mult  += card_mult_value(c)
            xmv = card_xmult_value(c)
            if xmv != 1.0:
                total_mult *= xmv
            total_mult += card_edition_mult_value(c)
            exmv = card_edition_xmult_value(c)
            if exmv != 1.0:
                total_mult *= exmv
        for c in (held_cards or []):
            if not is_debuffed(c) and _modifier(c).get("enhancement") == "STEEL":
                total_mult *= 1.5
        total = math.floor(total_chips * total_mult + 1e-9)
        return total_chips, total_mult, total

    from balatro_bot.joker_effects import ScoreContext, apply_joker_effects, retrigger_count

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
        deck_count=deck_count,
        deck_cards=deck_cards,
        pareidolia="j_pareidolia" in joker_keys_set,
        smeared="j_smeared" in joker_keys_set,
        ancient_suit=ancient_suit,
        blind_name=blind_name,
    )

    # Before phase: Vampire strips enhancements and pre-increments xmult
    effective_scoring, effective_played, vampire_xmult = _apply_before_phase(
        scoring_cards, played_cards, jokers)
    ctx.vampire_xmult = vampire_xmult

    _apply_card_scoring(ctx, effective_scoring, effective_played, jokers, ancient_suit)

    pre_joker_chips = ctx.chips
    pre_joker_mult = ctx.mult

    apply_joker_effects(ctx)

    total = math.floor(ctx.chips * ctx.mult + 1e-9)

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
    deck_count: int = 0,
    deck_cards: list[dict] | None = None,
    blind_name: str = "",
) -> dict:
    """Like score_hand but returns a full breakdown dict for logging."""
    from balatro_bot.joker_effects import ScoreContext, apply_joker_effects_detailed, retrigger_count

    base_chips, base_mult, _ = HAND_INFO[hand_name]
    if hand_levels and hand_name in hand_levels:
        lvl = hand_levels[hand_name]
        base_chips = lvl.get("chips", base_chips)
        base_mult = lvl.get("mult", base_mult)

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
        for c, (_, chips, mult, xmult) in zip(scoring_cards, card_details):
            total_chips += chips
            total_mult += mult
            if xmult != 1.0:
                total_mult *= xmult
            total_mult += card_edition_mult_value(c)
            exmv = card_edition_xmult_value(c)
            if exmv != 1.0:
                total_mult *= exmv
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
            "total": math.floor(total_chips * total_mult + 1e-9),
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
        deck_count=deck_count,
        deck_cards=deck_cards,
        pareidolia="j_pareidolia" in joker_keys_set,
        smeared="j_smeared" in joker_keys_set,
        ancient_suit=ancient_suit,
        blind_name=blind_name,
    )

    # Before phase: Vampire strips enhancements and pre-increments xmult
    effective_scoring, effective_played, vampire_xmult = _apply_before_phase(
        scoring_cards, played_cards, jokers)
    ctx.vampire_xmult = vampire_xmult

    _apply_card_scoring(ctx, effective_scoring, effective_played, jokers, ancient_suit)

    pre_joker_chips = ctx.chips
    pre_joker_mult = ctx.mult

    joker_contributions = apply_joker_effects_detailed(ctx)

    total = math.floor(ctx.chips * ctx.mult + 1e-9)
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
    """Return the subset of cards that actually score for the hand type.

    Stone cards always score when played (they contribute +50 chips regardless
    of hand type), so they're appended to the result even if they aren't part
    of the poker hand formation.
    """
    rc = _rank_counts(cards)
    counts_sorted = sorted(rc.items(), key=lambda x: (-x[1], -rank_value(x[0])))

    if hand_name in ("Flush", "Straight", "Straight Flush", "Flush Five", "Flush House"):
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

    # Stone cards always score when played — append any not already included
    result_set = set(id(c) for c in result)
    for c in cards:
        if is_stone(c) and id(c) not in result_set:
            result.append(c)

    return result


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
) -> list[HandCandidate]:
    """Enumerate all valid poker hands from the cards in hand."""
    candidates: list[HandCandidate] = []
    n = len(hand_cards)
    indices_set = set(range(n))

    joker_keys = {j.get("key") for j in (jokers or [])}
    four_fingers = "j_four_fingers" in joker_keys
    has_splash   = "j_splash" in joker_keys
    shortcut     = "j_shortcut" in joker_keys
    smeared      = "j_smeared" in joker_keys

    # Import play-order sorting so estimates match actual play order
    from balatro_bot.rules._helpers import _sort_play_order

    for size in range(min_select, min(max_select, n) + 1):
        for indices in combinations(range(n), size):
            if required_card_indices and not required_card_indices.issubset(set(indices)):
                continue
            subset = [hand_cards[i] for i in indices]
            hand_name = classify_hand(
                subset, four_fingers=four_fingers,
                shortcut=shortcut, smeared=smeared,
            )

            # Reorder played cards to match actual play order (additive first,
            # multiplicative last) so the score estimate reflects reality.
            if jokers:
                play_order = _sort_play_order(list(indices), hand_cards, jokers)
                played_in_order = [hand_cards[i] for i in play_order]
            else:
                played_in_order = subset

            scoring = subset if has_splash else _scoring_cards_for(hand_name, subset)
            held = [hand_cards[i] for i in indices_set - set(indices)] if jokers else []
            chips, mult, total = score_hand(
                hand_name, scoring, hand_levels,
                jokers=jokers, played_cards=played_in_order, held_cards=held,
                money=money, discards_left=discards_left, hands_left=hands_left,
                joker_limit=joker_limit, ancient_suit=ancient_suit,
                deck_count=deck_count, deck_cards=deck_cards,
                blind_name=blind_name,
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
    )
    return candidates[0] if candidates else None


# ---------------------------------------------------------------------------
# Discard analysis
# ---------------------------------------------------------------------------

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
        # Scoring suit: off-suit cards are more disposable
        1 if scoring_suit and scoring_suit in card_suits(hand_cards[i]) else 0 if not scoring_suit else -1,
        # Rank affinity: high → sort last (keep), negative → sort first (discard)
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
    best = best_hand(hand_cards, hand_levels, max_select, jokers=jokers, required_hand=required_hand)
    if not best:
        return [(list(range(min(max_discard, len(hand_cards)))), "no hand found")]

    # Extract utility joker flags for draw detection
    joker_keys = {j.get("key") for j in (jokers or [])}
    shortcut = "j_shortcut" in joker_keys
    smeared = "j_smeared" in joker_keys

    # Compute rank affinity for discard protection
    from balatro_bot.strategy import compute_strategy
    strat = compute_strategy(jokers or [], hand_levels)
    rank_aff = strat.rank_affinity_dict() or None

    strategies: list[tuple[str, list[int], float, str]] = []

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

    strategies.append((
        best.hand_name,
        best.card_indices,
        1.0,
        f"keep {best.hand_name}, discard dead cards",
    ))

    if HAND_INFO["Flush"][2] < HAND_INFO[best.hand_name][2]:
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

    if HAND_INFO["Straight"][2] < HAND_INFO[best.hand_name][2]:
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

    if deck_cards and HAND_INFO["Two Pair"][2] < HAND_INFO[best.hand_name][2]:
        tpdq = two_pair_draw_quality(hand_cards, deck_cards, rank_affinity=rank_aff)
        if tpdq:
            indices, prob = tpdq
            strategies.append((
                "Two Pair",
                indices,
                prob,
                f"chase Two Pair ({prob:.0%} to hit), discard {len(hand_cards) - len(indices)} cards",
            ))

    if deck_cards and HAND_INFO["Three of a Kind"][2] < HAND_INFO[best.hand_name][2]:
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
            elif hand_name != best.hand_name:
                base *= 0.3
        return base

    strategies.sort(key=chase_score, reverse=True)

    results: list[ChaseCandidate] = []
    has_blackboard = any(j.get("key") == "j_blackboard" for j in (jokers or []))

    for chase_name, keep, prob, reason in strategies:
        to_discard = cards_not_in(hand_cards, set(keep), blackboard=has_blackboard, rank_affinity=rank_aff)[:max_discard]
        if to_discard:
            results.append(ChaseCandidate(to_discard, reason, chase_name, list(keep), prob))

    return results
