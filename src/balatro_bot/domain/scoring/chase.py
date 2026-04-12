"""Chase strategy generation for the discard system.

Enumerates all viable hand-type transitions from the current hand state,
builds strategy tuples with keep sets and hit probabilities.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from balatro_bot.domain.models.deck_profile import DeckProfile

from balatro_bot.cards import joker_key
from balatro_bot.constants import HAND_INFO
from balatro_bot.domain.scoring.draws import (
    flush_draw,
    flush_draw_quality,
    flush_draw_quality_loose,
    straight_draw,
    straight_draw_quality,
    two_pair_draw_quality,
    two_pair_draw_quality_tight,
    three_kind_draw_quality,
    full_house_draw_quality,
    full_house_draw_quality_tight,
    four_kind_draw_quality,
    pair_draw_quality,
    straight_flush_draw_quality,
    flush_house_draw_quality,
    flush_five_draw_quality,
    five_kind_draw_quality,
)

# Strategy tuple: (hand_name, keep_indices, probability, reason)
StrategyTuple = tuple[str, list[int], float, str]


def generate_chases(
    hand_cards: list[dict],
    bh,
    hand_levels: dict[str, dict] | None = None,
    deck_cards: list[dict] | None = None,
    jokers: list[dict] | None = None,
    chips_remaining: int = 0,
    max_discard: int = 5,
    rank_affinity: dict[str, float] | None = None,
    required_hand: str | None = None,
    deck_profile: DeckProfile | None = None,
) -> list[StrategyTuple]:
    """Generate all viable chase strategies from the current hand state.

    Returns a list of (hand_name, keep_indices, probability, reason) tuples.
    Does NOT score or rank them — that's the caller's job.
    """
    joker_keys = {joker_key(j) for j in (jokers or [])}
    shortcut = "j_shortcut" in joker_keys
    smeared = "j_smeared" in joker_keys

    # When deck_cards is unavailable, use deck_profile for rough estimates
    def _fallback_flush_prob() -> float:
        """Estimate flush probability from deck suit concentration."""
        if deck_profile and deck_profile.suit_counts and deck_profile.total_cards > 0:
            best_suit_count = max(deck_profile.suit_counts.values())
            concentration = best_suit_count / deck_profile.total_cards
            # ~25% = balanced suits → ~30% hit; ~50%+ concentration → ~70% hit
            return min(0.8, max(0.15, concentration * 1.4))
        return 0.5

    def _fallback_straight_prob() -> float:
        """Estimate straight probability from deck rank spread."""
        if deck_profile and deck_profile.rank_counts and deck_profile.total_cards > 0:
            # More distinct ranks = more straight potential
            distinct = len(deck_profile.rank_counts)
            return min(0.6, max(0.2, distinct / 13 * 0.6))
        return 0.5

    strategies: list[StrategyTuple] = []

    # --- Redraw (desperation) ---
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

    # --- Keep current best hand ---
    strategies.append((
        bh.hand_name,
        bh.card_indices,
        1.0,
        f"keep {bh.hand_name}, discard dead cards",
    ))

    # --- Chase: upgrade to better hand types ---
    def _should_chase(target: str) -> bool:
        """True if target hand type ranks higher than current best."""
        return HAND_INFO[target][2] < HAND_INFO[bh.hand_name][2]

    # Pair (from High Card)
    if deck_cards and _should_chase("Pair"):
        result = pair_draw_quality(hand_cards, deck_cards, rank_affinity=rank_affinity)
        if result:
            indices, prob = result
            strategies.append((
                "Pair",
                indices,
                prob,
                f"chase Pair ({prob:.0%} to hit), discard {len(hand_cards) - len(indices)} cards",
            ))

    # Two Pair (from Pair) — loose: keep pair only; tight: keep pair + singleton
    if deck_cards and _should_chase("Two Pair"):
        result = two_pair_draw_quality(hand_cards, deck_cards, rank_affinity=rank_affinity)
        if result:
            indices, prob = result
            strategies.append((
                "Two Pair",
                indices,
                prob,
                f"chase Two Pair loose ({prob:.0%} to hit), discard {len(hand_cards) - len(indices)} cards",
            ))
        result_tight = two_pair_draw_quality_tight(hand_cards, deck_cards, rank_affinity=rank_affinity)
        if result_tight:
            indices, prob = result_tight
            strategies.append((
                "Two Pair",
                indices,
                prob,
                f"chase Two Pair tight ({prob:.0%} to hit), discard {len(hand_cards) - len(indices)} cards",
            ))

    # Three of a Kind (from Pair)
    if deck_cards and _should_chase("Three of a Kind"):
        result = three_kind_draw_quality(hand_cards, deck_cards, rank_affinity=rank_affinity)
        if result:
            indices, prob = result
            strategies.append((
                "Three of a Kind",
                indices,
                prob,
                f"chase Three of a Kind ({prob:.0%} to hit), discard {len(hand_cards) - len(indices)} cards",
            ))

    # Straight
    if _should_chase("Straight"):
        if deck_cards:
            result = straight_draw_quality(hand_cards, deck_cards, shortcut=shortcut)
            if result:
                indices, prob = result
                strategies.append((
                    "Straight",
                    indices,
                    prob,
                    f"chase Straight ({prob:.0%} to hit), discard {len(hand_cards) - len(indices)} cards",
                ))
        else:
            indices = straight_draw(hand_cards, shortcut=shortcut)
            if indices:
                prob = _fallback_straight_prob()
                strategies.append((
                    "Straight",
                    indices,
                    prob,
                    f"chase Straight (~{prob:.0%}), discard {len(hand_cards) - len(indices)} cards",
                ))

    # Flush — tight: keep 4 suited; loose: keep 3 suited (when only 3 available)
    if _should_chase("Flush"):
        if deck_cards:
            result = flush_draw_quality(hand_cards, deck_cards, smeared=smeared, rank_affinity=rank_affinity)
            if result:
                indices, prob, suit = result
                strategies.append((
                    "Flush",
                    indices,
                    prob,
                    f"chase Flush tight ({prob:.0%} to hit, {suit}), discard {len(hand_cards) - len(indices)} cards",
                ))
            result_loose = flush_draw_quality_loose(hand_cards, deck_cards, smeared=smeared, rank_affinity=rank_affinity)
            if result_loose:
                indices, prob, suit = result_loose
                strategies.append((
                    "Flush",
                    indices,
                    prob,
                    f"chase Flush loose ({prob:.0%} to hit, {suit}), discard {len(hand_cards) - len(indices)} cards",
                ))
        else:
            indices = flush_draw(hand_cards, smeared=smeared)
            if indices:
                prob = _fallback_flush_prob()
                strategies.append((
                    "Flush",
                    indices,
                    prob,
                    f"chase Flush (~{prob:.0%}), discard {len(hand_cards) - len(indices)} cards",
                ))

    # Full House — loose: trips only (any pair from deck); tight: trips + singleton (targeted)
    if deck_cards and _should_chase("Full House"):
        result = full_house_draw_quality(hand_cards, deck_cards, rank_affinity=rank_affinity)
        if result:
            indices, prob = result
            strategies.append((
                "Full House",
                indices,
                prob,
                f"chase Full House loose ({prob:.0%} to hit), discard {len(hand_cards) - len(indices)} cards",
            ))
        result_tight = full_house_draw_quality_tight(hand_cards, deck_cards, rank_affinity=rank_affinity)
        if result_tight:
            indices, prob = result_tight
            strategies.append((
                "Full House",
                indices,
                prob,
                f"chase Full House tight ({prob:.0%} to hit), discard {len(hand_cards) - len(indices)} cards",
            ))

    # Four of a Kind (from Three of a Kind)
    if deck_cards and _should_chase("Four of a Kind"):
        result = four_kind_draw_quality(hand_cards, deck_cards, rank_affinity=rank_affinity)
        if result:
            indices, prob = result
            strategies.append((
                "Four of a Kind",
                indices,
                prob,
                f"chase Four of a Kind ({prob:.0%} to hit), discard {len(hand_cards) - len(indices)} cards",
            ))

    # Five of a Kind (from Four of a Kind)
    if deck_cards and _should_chase("Five of a Kind"):
        result = five_kind_draw_quality(hand_cards, deck_cards)
        if result:
            indices, prob = result
            strategies.append((
                "Five of a Kind",
                indices,
                prob,
                f"chase Five of a Kind ({prob:.0%} to hit), discard {len(hand_cards) - len(indices)} cards",
            ))

    # Straight Flush
    if deck_cards and _should_chase("Straight Flush"):
        result = straight_flush_draw_quality(
            hand_cards, deck_cards, shortcut=shortcut, smeared=smeared,
        )
        if result:
            indices, prob = result
            strategies.append((
                "Straight Flush",
                indices,
                prob,
                f"chase Straight Flush ({prob:.0%} to hit), discard {len(hand_cards) - len(indices)} cards",
            ))

    # Flush House (Flush + Full House)
    if deck_cards and _should_chase("Flush House"):
        result = flush_house_draw_quality(
            hand_cards, deck_cards, smeared=smeared, rank_affinity=rank_affinity,
        )
        if result:
            indices, prob = result
            strategies.append((
                "Flush House",
                indices,
                prob,
                f"chase Flush House ({prob:.0%} to hit), discard {len(hand_cards) - len(indices)} cards",
            ))

    # Flush Five (Flush + Five of a Kind)
    if deck_cards and _should_chase("Flush Five"):
        result = flush_five_draw_quality(
            hand_cards, deck_cards, smeared=smeared, rank_affinity=rank_affinity,
        )
        if result:
            indices, prob = result
            strategies.append((
                "Flush Five",
                indices,
                prob,
                f"chase Flush Five ({prob:.0%} to hit), discard {len(hand_cards) - len(indices)} cards",
            ))

    # --- Filter by required hand (The Mouth boss) ---
    if required_hand:
        strategies = [
            s for s in strategies
            if s[0] == required_hand or s[0] == "redraw"
        ]

    return strategies
