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

import copy
from dataclasses import replace as dc_replace

from balatro_bot.actions import DiscardCards, Action
from balatro_bot.cards import joker_key
from balatro_bot.domain.models.joker import Joker
from balatro_bot.domain.scoring.search import (
    best_hand, cards_not_in, discard_candidates, ChaseCandidate,
)
from balatro_bot.joker_effects.parsers import _ab_mult, _ab_xmult

if TYPE_CHECKING:
    from balatro_bot.context import RoundContext

log = logging.getLogger("balatro_bot")

N_SAMPLES = 30          # Monte Carlo samples per unique keep set


# Jokers whose accumulated ability value DECAYS on discard. The scoring sim
# reads their live values from the joker dict, so without adjustment the MC
# over-estimates a chase's future-hand EV (the bot "sees" the pre-discard
# Ramen xmult when it should see the post-discard value). We clone these
# jokers with decremented ability fields before passing to best_hand.
#
# Incrementing-on-discard jokers (Yorick, Castle, Hit the Road, Trading,
# Mail-in Rebate, Faceless, Burnt) are NOT handled here — they make the sim
# under-estimate chase value, which is a conservativeness bias but not a
# correctness bug. Add them here later if tuning shows the bot under-uses
# discards in those builds.
_DISCARD_DECAY_JOKERS = frozenset({"j_green_joker", "j_ramen"})


def _adjust_jokers_for_discard(jokers: list, discard_count: int) -> list:
    """Return a joker list with decay-on-discard jokers decremented.

    Handles both typed Joker dataclasses (the actual runtime case) and raw
    dicts (legacy/tests). Green Joker loses 1 flat mult (floor 0); Ramen
    loses 0.01 xmult per discarded card (floor 1.0 — below X1 Ramen stops
    firing).
    """
    out = []
    for j in jokers:
        key = getattr(j, "key", None) if not isinstance(j, dict) else j.get("key")
        if key not in _DISCARD_DECAY_JOKERS:
            out.append(j)
            continue

        if isinstance(j, Joker):
            if key == "j_green_joker":
                cur = _ab_mult(j, fallback=0)
                new_ability = dc_replace(j.value.ability, mult=max(0.0, cur - 1.0))
            else:  # j_ramen
                cur = _ab_xmult(j, fallback=1.85)
                new_ability = dc_replace(
                    j.value.ability, x_mult=max(1.0, cur - 0.01 * discard_count)
                )
            new_value = dc_replace(j.value, ability=new_ability)
            out.append(dc_replace(j, value=new_value))
        else:
            new_j = copy.deepcopy(j)
            ability = new_j.setdefault("value", {}).setdefault("ability", {})
            if key == "j_green_joker":
                cur = _ab_mult(new_j, fallback=0)
                ability["mult"] = max(0.0, cur - 1.0)
            else:
                cur = _ab_xmult(new_j, fallback=1.85)
                ability["x_mult"] = max(1.0, cur - 0.01 * discard_count)
            out.append(new_j)
    return out


def _discard_size_cap(ctx: RoundContext) -> int:
    """Max cards per discard based on boss blind.

    The Serpent: only 3 cards drawn back per play/discard. Discarding 1 nets
    +2 cards (free deck cycling); discarding N>=2 is neutral-to-negative.
    Cap at 1 unless a specific trigger requires more (none currently tracked).
    """
    if ctx.blind_name == "The Serpent":
        return 1
    return 5


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
            max_discard=min(5, ctx.discards_left, _discard_size_cap(ctx)),
            deck_cards=ctx.deck_cards,
            chips_remaining=ctx.chips_remaining,
            jokers=ctx.jokers,
            required_hand=ctx.mouth_locked_hand or ctx.committed_hand_type,
            protection=ctx.card_protection,
        )
        best_chase = _best_chase(suggestions, ctx, play_ev)
        if best_chase is not None:
            return best_chase

        # Desperation cycle: extra cards in hand + hopeless outlook
        extra_count = len(ctx.hand_cards) - 5
        if extra_count > 0 and outlook == "hopeless":
            extras = cards_not_in(ctx.hand_cards, set(ctx.best.card_indices), protection=ctx.card_protection)
            to_discard = extras[:min(extra_count, ctx.discards_left, _discard_size_cap(ctx))]
            if to_discard:
                return DiscardCards(
                    to_discard,
                    reason=f"desperation cycle ({outlook}): {ctx.best.hand_name} for {ctx.best.total} vs {ctx.chips_remaining} needed",
                )
        return None

    suggestions = discard_candidates(
        ctx.hand_cards, ctx.hand_levels,
        max_discard=min(5, ctx.discards_left, _discard_size_cap(ctx)),
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

    # Decrement decay-on-discard jokers (Green Joker, Ramen) to reflect the
    # hypothetical discard we're about to model. Without this, the sim reads
    # the pre-discard accumulated value and over-estimates future-hand EV.
    sim_jokers = _adjust_jokers_for_discard(ctx.jokers, discard_count)

    total = 0.0
    for _ in range(N_SAMPLES):
        drawn = random.sample(draw_pile, discard_count)
        new_hand = keep_cards + drawn
        result = best_hand(
            new_hand,
            hand_levels=ctx.hand_levels,
            jokers=sim_jokers,
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
_BASE_CHASE_MARGIN = 1.25       # chase must be at least 1.4× play_ev
_SCARCITY_BONUS_PER = 0.15     # +0.15× for each discard already used

_HAND_ABBREV = {
    "High Card": "HC", "Pair": "Pair", "Two Pair": "TP",
    "Three of a Kind": "3oK", "Straight": "Str", "Flush": "Flush",
    "Full House": "FH", "Four of a Kind": "4oK", "Straight Flush": "SF",
    "Five of a Kind": "5oK", "Flush House": "FlH", "Flush Five": "Fl5",
}


def _chase_margin(ctx: RoundContext, chase_hand: str | None = None) -> float:
    """Required EV multiplier for a chase to be accepted.

    Base margin of 1.25× increases as discards get scarcer.
    With 3+ discards left the bar is low; with 1 left it's steep.
    Hopeless outlook lowers the bar — we're desperate.

    If *chase_hand* matches the roster's preferred hand types (via
    ``ctx.strategy``), the margin is relaxed proportionally — a Tribe/Order
    roster should chase Flush/Straight more aggressively than a chip-heavy
    one. Reduction caps at 20% of the margin so we never chase blindly.
    """
    if ctx.discards_left >= 3:
        margin = _BASE_CHASE_MARGIN
    elif ctx.discards_left == 2:
        margin = _BASE_CHASE_MARGIN + _SCARCITY_BONUS_PER
    else:
        margin = _BASE_CHASE_MARGIN + _SCARCITY_BONUS_PER * 2

    if ctx.round_outlook == "hopeless":
        margin = max(1.05, margin * 0.7)

    if chase_hand and ctx.strategy is not None:
        affinity = ctx.strategy.hand_affinity(chase_hand)
        if affinity > 0:
            # affinity is the raw sum of JOKER_HAND_AFFINITY weights for this
            # hand from the roster. Typical ranges: 1-3 = moderate interest,
            # 4-8 = strong preference. Cap discount at 0.8× margin.
            discount = min(0.2, affinity * 0.04)
            margin = max(1.05, margin * (1.0 - discount))

    return margin


def _best_chase(suggestions: list[ChaseCandidate], ctx: RoundContext, play_ev: float) -> DiscardCards | None:
    """Pick the chase candidate with highest MC EV above the margin threshold.

    One MC pass per unique keep set — multiple candidate labels (different
    chase_hand for the same kept cards) share the same realized EV, so they
    dedupe. The chase_hand label is kept only for logging.
    """
    base_margin_req = _chase_margin(ctx)  # for logging only
    ev_cache: dict[tuple[int, ...], float] = {}

    def ev_for(candidate: ChaseCandidate) -> float:
        key = tuple(sorted(candidate.keep_indices))
        if key not in ev_cache:
            ev_cache[key] = _expected_play_value(candidate.keep_indices, ctx)
        return ev_cache[key]

    best = None
    best_ev = 0.0

    chase_parts: list[str] = []
    for candidate in suggestions:
        if "chase" not in candidate.reason:
            continue
        # Per-chase margin: preferred-hand chases get a relaxed threshold
        margin_req = _chase_margin(ctx, candidate.chase_hand)
        ev_threshold = play_ev * margin_req
        ev = ev_for(candidate)
        # A chase is accepted if it beats its own threshold AND is the best
        # among accepted chases so far.
        accepted = ev > ev_threshold and ev > best_ev
        abbrev = _HAND_ABBREV.get(candidate.chase_hand, candidate.chase_hand)
        chase_parts.append(
            f"{abbrev} {candidate.hit_prob * 100:.0f}% EV {ev:.0f} (need {ev_threshold:.0f}) {'YES' if accepted else 'no'}"
        )
        log.info(
            "chase EV: %s %.0f%% -> EV %.0f (threshold=%.0f, margin=%.2fx) %s",
            candidate.chase_hand, candidate.hit_prob * 100, ev,
            ev_threshold, margin_req, "ACCEPT" if accepted else "reject",
        )
        if accepted:
            best_ev = ev
            best = candidate

    margin_req = base_margin_req  # for final logs below
    if chase_parts:
        _stream_log.info("Considering chase: %s (play EV %.0f, base need %.1fx)", " | ".join(chase_parts), play_ev, base_margin_req)

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
