"""Pre-lock commitment for The Mouth: pick the best round-total hand type.

The Mouth locks your hand type on the first played hand. Before that lock,
we pick the type that maximizes expected round total (first play + repeated
follow-ups), not the type with the best single-hand score.

v1 baseline. PR2 refines the repeatability model with Four Fingers / Smeared /
Wild / Shortcut awareness and deck-composition-aware flush/straight p_repeat.
"""

from __future__ import annotations

import logging

from balatro_bot.domain.scoring.search import enumerate_hands

log = logging.getLogger("balatro_bot")


# v1 repeatability: P(reforming this hand type on a fresh 8-card draw).
# Stock deck baseline, no joker modifiers. PR2 refines.
_REPEATABILITY_BASELINE: dict[str, float] = {
    "High Card": 1.0,
    "Pair": 1.0,
    "Two Pair": 0.85,
    "Three of a Kind": 0.45,
    "Straight": 0.25,
    "Flush": 0.30,
    "Full House": 0.10,
    "Four of a Kind": 0.03,
    "Straight Flush": 0.01,
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

    best_type: str | None = None
    best_round_total = -1.0

    for hand_type, cand in best_per_type.items():
        p_repeat = _repeatability(hand_type, deck_profile, jokers)
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


def _repeatability(hand_type: str, deck_profile, jokers) -> float:
    """P(forming `hand_type` on a fresh 8-card draw).

    v1 baseline. PR2 refines for Four Fingers, Smeared, Wild, Shortcut, and
    deck suit/rank composition.
    """
    return _REPEATABILITY_BASELINE.get(hand_type, 0.05)
