"""Discard-phase policy functions — pure decision logic extracted from rules.

Each function takes a RoundContext, returns an Action or None.
DiscardToImprove in rules/playing.py becomes a thin wrapper.

Ranking is done via a single Monte Carlo pass per unique keep set. For each
candidate keep set, we sample draws from the deck, score the resulting hand
with full joker effects via best_hand(), and average. This subsumes the
previous three-pass (chase_score + _chase_ev + _sample_miss_ev) design.
"""

from __future__ import annotations

import logging
import random

_stream_log = logging.getLogger("balatro_stream")
from typing import TYPE_CHECKING

from balatro_bot.actions import DiscardCards, Action
from balatro_bot.cards import joker_key
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

N_SAMPLES = 30          # Monte Carlo samples per unique keep set


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
        suggestions = discard_candidates(
            ctx.hand_cards, ctx.hand_levels,
            max_discard=min(5, ctx.discards_left),
            deck_cards=ctx.deck_cards,
            chips_remaining=ctx.chips_remaining,
            jokers=ctx.jokers,
            required_hand=ctx.mouth_locked_hand,
            protection=ctx.card_protection,
        )
        best_chase = _best_chase(suggestions, ctx, play_ev)
        if best_chase is not None:
            return best_chase

        # Desperation cycle: extra cards in hand + hopeless outlook
        extra_count = len(ctx.hand_cards) - 5
        if extra_count > 0 and outlook == "hopeless":
            extras = cards_not_in(ctx.hand_cards, set(ctx.best.card_indices), protection=ctx.card_protection)
            to_discard = extras[:min(extra_count, ctx.discards_left, 5)]
            if to_discard:
                return DiscardCards(
                    to_discard,
                    reason=f"desperation cycle ({outlook}): {ctx.best.hand_name} for {ctx.best.total} vs {ctx.chips_remaining} needed",
                )
        return None

    joker_keys = {joker_key(j) for j in ctx.jokers}

    # If we have jokers that reward keeping discards, be conservative
    has_keep_discard_jokers = bool(joker_keys & KEEP_DISCARDS_JOKERS)
    if has_keep_discard_jokers and outlook != "hopeless":
        return None

    suggestions = discard_candidates(
        ctx.hand_cards, ctx.hand_levels,
        max_discard=min(5, ctx.discards_left),
        deck_cards=ctx.deck_cards,
        chips_remaining=ctx.chips_remaining,
        jokers=ctx.jokers,
        required_hand=ctx.mouth_locked_hand,
        deck_profile=ctx.deck_profile,
        protection=ctx.card_protection,
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


def _expected_play_value(keep_indices: list[int], ctx: RoundContext) -> float:
    """Monte Carlo estimate of best_hand() value after discarding non-kept cards.

    Samples `discard_count` cards from the deck, forms a new hand with the kept
    cards, and scores best_hand() with full joker effects. Averages over
    N_SAMPLES samples to produce an expected realized score.

    Single unified primitive — replaces the old (chase_score + _chase_ev +
    _sample_miss_ev) trio. Because the sample uniformly explores both hit and
    miss outcomes, the hit/miss split is captured implicitly by the mean.
    """
    keep_cards = [ctx.hand_cards[i] for i in keep_indices]
    discard_count = len(ctx.hand_cards) - len(keep_cards)
    draw_pile = ctx.deck_cards

    if not draw_pile or len(draw_pile) < discard_count:
        return ctx.best.total if ctx.best else 0.0

    total = 0.0
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
            idol_rank=ctx.idol_rank,
            idol_suit=ctx.idol_suit,
            deck_cards=ctx.deck_cards,
            blind_name=ctx.blind_name,
        )
        total += result.total if result else 0
    return total / N_SAMPLES


# Minimum EV multiplier a chase must beat play_ev by.
# Scales with discard scarcity: burning your last discard needs a bigger payoff.
_BASE_CHASE_MARGIN = 1.4       # chase must be at least 1.4× play_ev
_SCARCITY_BONUS_PER = 0.15     # +0.15× for each discard already used

_HAND_ABBREV = {
    "High Card": "HC", "Pair": "Pair", "Two Pair": "TP",
    "Three of a Kind": "3oK", "Straight": "Str", "Flush": "Flush",
    "Full House": "FH", "Four of a Kind": "4oK", "Straight Flush": "SF",
    "Five of a Kind": "5oK", "Flush House": "FlH", "Flush Five": "Fl5",
}


def _chase_margin(ctx: RoundContext) -> float:
    """Required EV multiplier for a chase to be accepted.

    Base margin of 1.4× increases as discards get scarcer.
    With 3+ discards left the bar is low; with 1 left it's steep.
    Hopeless outlook lowers the bar — we're desperate.
    """
    if ctx.discards_left >= 3:
        margin = _BASE_CHASE_MARGIN
    elif ctx.discards_left == 2:
        margin = _BASE_CHASE_MARGIN + _SCARCITY_BONUS_PER
    else:
        margin = _BASE_CHASE_MARGIN + _SCARCITY_BONUS_PER * 2

    if ctx.round_outlook == "hopeless":
        margin = max(1.05, margin * 0.7)

    return margin


def _best_chase(suggestions: list[ChaseCandidate], ctx: RoundContext, play_ev: float) -> DiscardCards | None:
    """Pick the chase candidate with highest MC EV above the margin threshold.

    One MC pass per unique keep set — multiple candidate labels (different
    chase_hand for the same kept cards) share the same realized EV, so they
    dedupe. The chase_hand label is kept only for logging.
    """
    margin_req = _chase_margin(ctx)
    ev_threshold = play_ev * margin_req

    ev_cache: dict[tuple[int, ...], float] = {}

    def ev_for(candidate: ChaseCandidate) -> float:
        key = tuple(sorted(candidate.keep_indices))
        if key not in ev_cache:
            ev_cache[key] = _expected_play_value(candidate.keep_indices, ctx)
        return ev_cache[key]

    best = None
    best_ev = ev_threshold

    chase_parts: list[str] = []
    for candidate in suggestions:
        if "chase" not in candidate.reason:
            continue
        ev = ev_for(candidate)
        accepted = ev > best_ev
        abbrev = _HAND_ABBREV.get(candidate.chase_hand, candidate.chase_hand)
        chase_parts.append(
            f"{abbrev} {candidate.hit_prob * 100:.0f}% EV {ev:.0f} {'YES' if accepted else 'no'}"
        )
        log.info(
            "chase EV: %s %.0f%% -> EV %.0f (threshold=%.0f) %s",
            candidate.chase_hand, candidate.hit_prob * 100, ev,
            ev_threshold, "ACCEPT" if accepted else "reject",
        )
        if accepted:
            best_ev = ev
            best = candidate

    if chase_parts:
        _stream_log.info("Considering chase: %s (play EV %.0f, need %.1fx)", " | ".join(chase_parts), play_ev, margin_req)

    if best is not None:
        margin = best_ev / play_ev if play_ev > 0 else float("inf")
        log.info("chase ACCEPTED: %s EV %.0f vs play %.0f (%.1fx, needed %.1fx)", best.chase_hand, best_ev, play_ev, margin, margin_req)
        return DiscardCards(
            best.discard_indices,
            reason=f"{best.reason} [EV {best_ev:.0f} vs play {play_ev:.0f}, {margin:.1f}x]",
        )
    if ev_cache:
        log.info("all chases rejected (play_ev=%.0f, threshold=%.0f, margin=%.2fx)", play_ev, ev_threshold, margin_req)
    return None
