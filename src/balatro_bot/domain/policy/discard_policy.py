"""Discard-phase policy functions — pure decision logic extracted from rules.

Each function takes a RoundContext, returns an Action or None.
DiscardToImprove in rules/playing.py becomes a thin wrapper.
"""

from __future__ import annotations

import logging
import random
from typing import TYPE_CHECKING

from balatro_bot.actions import DiscardCards, Action
from balatro_bot.domain.scoring.estimate import score_hand
from balatro_bot.domain.scoring.search import (
    best_hand, cards_not_in, discard_candidates, ChaseCandidate,
)

if TYPE_CHECKING:
    from balatro_bot.context import RoundContext

log = logging.getLogger("balatro_bot")

# Jokers that LOSE value when discards are used.
KEEP_DISCARDS_JOKERS = {
    "j_banner",         # +30 chips per discard remaining
    "j_delayed_grat",   # $2 per unused discard
    "j_green_joker",    # -1 mult per discard
    "j_ramen",          # -0.01 xmult per card discarded
}

N_SAMPLES = 10  # Monte Carlo samples per unique keep set


def choose_discard(ctx: RoundContext) -> Action | None:
    """Decide whether and what to discard to improve the hand.

    Uses Monte Carlo sampling for expected value comparison:
      chase_ev = hit_prob * improved_score + (1 - hit_prob) * miss_ev
      play_ev  = current_best_score
    """
    if ctx.discards_left <= 0:
        return None
    if not ctx.best:
        return None
    # Only discard if the current best hand can't clear the blind
    if ctx.best.total >= ctx.chips_remaining:
        return None

    outlook = ctx.round_outlook
    play_ev = ctx.best.total
    log.info(
        "DiscardToImprove: outlook=%s, best=%s for %d, chips_remaining=%d, hands=%d, discards=%d",
        outlook, ctx.best.hand_name, play_ev, ctx.chips_remaining, ctx.hands_left, ctx.discards_left,
    )

    # If best hand is already 5 cards, only chase draws matter
    if len(ctx.best.card_indices) >= 5:
        strat_affinity = {ht: score for ht, score in ctx.strategy.preferred_hands}
        suggestions = discard_candidates(
            ctx.hand_cards, ctx.hand_levels,
            max_discard=min(5, ctx.discards_left),
            strategy_affinity=strat_affinity,
            deck_cards=ctx.deck_cards,
            chips_remaining=ctx.chips_remaining,
            jokers=ctx.jokers,
            required_hand=ctx.mouth_locked_hand,
        )
        best_chase = _best_chase(suggestions, ctx, play_ev)
        if best_chase is not None:
            return best_chase

        # Desperation cycle: extra cards in hand + hopeless outlook
        extra_count = len(ctx.hand_cards) - 5
        if extra_count > 0 and outlook == "hopeless":
            extras = cards_not_in(ctx.hand_cards, set(ctx.best.card_indices), rank_affinity=ctx.strategy.rank_affinity_dict(), scoring_suit=ctx.scoring_suit)
            to_discard = extras[:min(extra_count, ctx.discards_left, 5)]
            if to_discard:
                return DiscardCards(
                    to_discard,
                    reason=f"desperation cycle ({outlook}): {ctx.best.hand_name} for {ctx.best.total} vs {ctx.chips_remaining} needed",
                )
        return None

    joker_keys = {j.get("key") for j in ctx.jokers}

    # If we have jokers that reward keeping discards, be conservative
    has_keep_discard_jokers = bool(joker_keys & KEEP_DISCARDS_JOKERS)
    if has_keep_discard_jokers and outlook != "hopeless":
        return None

    strat_affinity = {ht: score for ht, score in ctx.strategy.preferred_hands}

    suggestions = discard_candidates(
        ctx.hand_cards, ctx.hand_levels,
        max_discard=min(5, ctx.discards_left),
        strategy_affinity=strat_affinity,
        deck_cards=ctx.deck_cards,
        chips_remaining=ctx.chips_remaining,
        jokers=ctx.jokers,
        required_hand=ctx.mouth_locked_hand,
    )

    # Try EV-based chase first
    best_chase_result = _best_chase(suggestions, ctx, play_ev)
    if best_chase_result is not None:
        return best_chase_result

    # Discard dead cards when hopeless or tight AND hand uses < 5 cards
    if outlook in ("hopeless", "tight") and len(ctx.best.card_indices) < 5:
        for candidate in suggestions:
            if "chase" not in candidate.reason:
                return DiscardCards(candidate.discard_indices, reason=candidate.reason)

    # Last resort: hopeless outlook, hand < 5 cards
    if suggestions and outlook == "hopeless" and len(ctx.best.card_indices) < 5:
        candidate = suggestions[0]
        return DiscardCards(candidate.discard_indices, reason=f"discard to improve ({outlook}): {candidate.reason}")

    return None


def _sample_miss_ev(keep_indices: list[int], ctx: RoundContext) -> float:
    """Monte Carlo estimate of hand value after a failed chase."""
    keep_cards = [ctx.hand_cards[i] for i in keep_indices]
    discard_count = len(ctx.hand_cards) - len(keep_cards)
    draw_pile = ctx.deck_cards

    if not draw_pile or len(draw_pile) < discard_count:
        return ctx.best.total if ctx.best else 0

    total = 0
    for _ in range(N_SAMPLES):
        drawn = random.sample(draw_pile, discard_count)
        new_hand = keep_cards + drawn
        result = best_hand(
            new_hand,
            hand_levels=ctx.hand_levels,
            jokers=ctx.jokers,
            money=ctx.money,
            discards_left=max(0, ctx.discards_left - 1),
            hands_left=ctx.hands_left,
            ancient_suit=ctx.ancient_suit,
        )
        total += result.total if result else 0
    return total / N_SAMPLES


def _chase_ev(candidate: ChaseCandidate, ctx: RoundContext, miss_ev: float) -> float:
    """Expected value of taking a chase discard."""
    if candidate.chase_hand == "redraw":
        return miss_ev

    keep_cards = [ctx.hand_cards[i] for i in candidate.keep_indices]
    _, _, improved = score_hand(
        candidate.chase_hand,
        keep_cards,
        hand_levels=ctx.hand_levels,
        jokers=ctx.jokers,
        played_cards=keep_cards,
        held_cards=[],
        money=ctx.money,
        discards_left=max(0, ctx.discards_left - 1),
        hands_left=ctx.hands_left,
        ancient_suit=ctx.ancient_suit,
    )

    return candidate.hit_prob * improved + (1 - candidate.hit_prob) * miss_ev


def _best_chase(suggestions: list[ChaseCandidate], ctx: RoundContext, play_ev: float) -> DiscardCards | None:
    """Find the best chase candidate whose EV exceeds play_ev."""
    # Build miss_ev cache — one sample pass per unique keep set
    miss_ev_cache: dict[tuple[int, ...], float] = {}
    for candidate in suggestions:
        if "chase" not in candidate.reason:
            continue
        key = tuple(sorted(candidate.keep_indices))
        if key not in miss_ev_cache:
            miss_ev_cache[key] = _sample_miss_ev(candidate.keep_indices, ctx)
            log.info("MC miss_ev for keep=%s: %.0f (play_ev=%.0f)", key, miss_ev_cache[key], play_ev)

    # Find best chase by EV
    best = None
    best_ev = play_ev

    for candidate in suggestions:
        if "chase" not in candidate.reason:
            continue
        key = tuple(sorted(candidate.keep_indices))
        ev = _chase_ev(candidate, ctx, miss_ev_cache[key])
        log.info(
            "chase EV: %s %.0f%% -> EV %.0f (miss=%.0f, play=%.0f) %s",
            candidate.chase_hand, candidate.hit_prob * 100, ev,
            miss_ev_cache[key], play_ev,
            "ACCEPT" if ev > best_ev else "reject",
        )
        if ev > best_ev:
            best_ev = ev
            best = candidate

    if best is not None:
        margin = best_ev / play_ev if play_ev > 0 else float("inf")
        log.info("chase ACCEPTED: %s EV %.0f vs play %.0f (%.1fx)", best.chase_hand, best_ev, play_ev, margin)
        return DiscardCards(
            best.discard_indices,
            reason=f"{best.reason} [EV {best_ev:.0f} vs play {play_ev:.0f}, {margin:.1f}x]",
        )
    if miss_ev_cache:
        log.info("all chases rejected (play_ev=%.0f beats all)", play_ev)
    return None
