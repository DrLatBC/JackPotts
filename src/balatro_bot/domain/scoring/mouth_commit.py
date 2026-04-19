"""Pre-lock commitment for The Mouth: pick the best round-total hand type.

The Mouth locks your hand type on the first played hand. Before that lock,
we pick the type that maximizes expected round total (first play + repeated
follow-ups), not the type with the best single-hand score.

v1 baseline. PR2 refines the repeatability model with Four Fingers / Smeared /
Wild / Shortcut awareness and deck-composition-aware flush/straight p_repeat.
"""

from __future__ import annotations

import logging
from math import comb

from balatro_bot.cards import joker_key
from balatro_bot.domain.scoring.search import enumerate_hands

log = logging.getLogger("balatro_bot")

_HAND_SIZE = 8  # standard draw; close enough for ranking purposes


# P(reforming each hand type on a fresh 8-card draw). Values are static
# baselines used when deck composition doesn't meaningfully shift the answer
# (pair-class hands are near-universal in normal decks).
_REPEAT_STATIC: dict[str, float] = {
    "High Card": 1.0,
    "Pair": 1.0,
    "Two Pair": 0.85,
    "Three of a Kind": 0.45,
    "Full House": 0.10,
    "Four of a Kind": 0.03,
    "Royal Flush": 0.005,
    "Five of a Kind": 0.0,
    "Flush House": 0.0,
    "Flush Five": 0.0,
}


def choose_mouth_commit(
    hand_cards,
    hand_levels,
    jokers,
    joker_limit: int,
    hands_left: int,
    deck_cards,
    deck_profile,
    ancient_suit,
    idol_rank,
    idol_suit,
    forced_card_idx,
    blind_name: str,
    ox_most_played,
) -> str | None:
    """Pick the hand type with the best round-total EV under Mouth pre-lock.

    Returns the committed hand type, or None if nothing is formable from the
    current hand. v1 only considers types formable now — unformable types
    (would require discarding) are deferred to PR2.
    """
    if hands_left <= 0:
        return None

    candidates = enumerate_hands(
        hand_cards, hand_levels,
        jokers=jokers, joker_limit=joker_limit,
        hands_left=hands_left,
        required_card_indices={forced_card_idx} if forced_card_idx is not None else None,
        ancient_suit=ancient_suit,
        deck_cards=deck_cards,
        blind_name=blind_name,
        ox_most_played=ox_most_played,
        idol_rank=idol_rank,
        idol_suit=idol_suit,
    )
    if not candidates:
        return None

    best_per_type: dict[str, object] = {}
    for c in candidates:
        existing = best_per_type.get(c.hand_name)
        if existing is None or c.total > existing.total:
            best_per_type[c.hand_name] = c

    joker_keys_set = {joker_key(j) for j in (jokers or [])}

    best_type: str | None = None
    best_round_total = -1.0

    for hand_type, cand in best_per_type.items():
        p_repeat = _repeatability(hand_type, deck_profile, joker_keys_set)
        first_score = cand.total
        follow_score = first_score  # v1 approximation: follow hand ≈ first hand
        round_total = first_score + max(0, hands_left - 1) * p_repeat * follow_score
        if round_total > best_round_total:
            best_round_total = round_total
            best_type = hand_type

    if best_type is not None:
        log.info(
            "MouthCommit: %s (round_total=%d) from %d formable types",
            best_type, int(best_round_total), len(best_per_type),
        )

    return best_type


def _repeatability(hand_type: str, deck_profile, joker_keys_set: set[str]) -> float:
    """P(forming `hand_type` on a fresh 8-card draw from the current deck."""
    if hand_type in _REPEAT_STATIC:
        return _REPEAT_STATIC[hand_type]

    four_fingers = "j_four_fingers" in joker_keys_set
    shortcut = "j_shortcut" in joker_keys_set
    smeared = "j_smeared" in joker_keys_set

    if hand_type == "Flush":
        return _flush_repeatability(deck_profile, four_fingers, smeared)
    if hand_type == "Straight":
        # Rough bump for shortcut (gapped straights ~double the valid windows).
        # Four Fingers makes 4-card straights count, also ~doubles.
        base = 0.25
        if shortcut:
            base = min(1.0, base * 2.0)
        if four_fingers:
            base = min(1.0, base * 1.8)
        return base
    if hand_type == "Straight Flush":
        base = 0.01
        if shortcut:
            base *= 2
        if four_fingers:
            base *= 3  # 4-card straight flush is vastly easier
        return min(base, 0.15)
    return 0.05


def _flush_repeatability(deck_profile, four_fingers: bool, smeared: bool) -> float:
    """P(drawing 4 or 5 cards of the same suit in an 8-card draw)."""
    if deck_profile is None:
        return 0.6 if four_fingers else 0.30

    total = deck_profile.total_cards or sum(deck_profile.suit_counts.values())
    if total <= 0:
        return 0.0
    if not deck_profile.suit_counts:
        return 0.6 if four_fingers else 0.30

    # Smeared collapses H+D and C+S. Wild cards already contribute to every
    # suit in suit_counts (DeckProfile.from_cards handles that).
    if smeared:
        groups = {
            "red": deck_profile.suit_counts.get("H", 0) + deck_profile.suit_counts.get("D", 0),
            "black": deck_profile.suit_counts.get("C", 0) + deck_profile.suit_counts.get("S", 0),
        }
    else:
        groups = deck_profile.suit_counts

    max_suit = max(groups.values()) if groups else 0
    if max_suit <= 0:
        return 0.0

    need = 4 if four_fingers else 5
    draws = min(_HAND_SIZE, total)
    good = max_suit
    bad = total - good
    if good < need or draws < need:
        return 0.0

    total_combos = comb(total, draws)
    if total_combos == 0:
        return 0.0

    # Hypergeometric tail: P(X >= need) = 1 - sum_{k<need} C(good,k)*C(bad,draws-k)/C(total,draws)
    p_below = 0.0
    for k in range(need):
        if k <= good and (draws - k) <= bad:
            p_below += comb(good, k) * comb(bad, draws - k) / total_combos

    return max(0.0, min(1.0, 1.0 - p_below))
