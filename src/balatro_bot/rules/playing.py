from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from balatro_bot.actions import PlayCards, DiscardCards, SellJoker, Action
from balatro_bot.context import RoundContext
from balatro_bot.constants import (
    SCALING_JOKERS, PLAY_SCALERS, FINAL_HAND_JOKERS,
    DISCARD_SCALERS, FACE_RANKS_SET,
)
from balatro_bot.cards import card_rank, rank_value, is_debuffed
from balatro_bot.hand_evaluator import best_hand, cards_not_in, discard_candidates
from balatro_bot.rules._helpers import _pad_with_junk

if TYPE_CHECKING:
    from typing import Any

log = logging.getLogger("balatro_bot")


class VerdantLeafUnlock:
    """Sell weakest joker to lift Verdant Leaf's full-debuff on all cards.

    Verdant Leaf debuffs every playing card until one joker is sold.
    Fires immediately on the first SELECTING_HAND tick — sell the cheapest
    non-scaling joker, then normal play resumes.
    """
    name = "verdant_leaf_unlock"

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        ctx = RoundContext.from_state(state)
        if ctx.blind_name != "Verdant Leaf":
            return None
        # Check if debuff is still active (any hand card is debuffed)
        if not any(is_debuffed(c) for c in ctx.hand_cards):
            return None
        # Find the weakest joker to sacrifice (lowest sell value, non-scaling first)
        candidates = [
            (i, j) for i, j in enumerate(ctx.jokers)
            if j.get("key") not in SCALING_JOKERS
        ]
        if not candidates:
            # All jokers are scaling — sell the cheapest one anyway
            candidates = list(enumerate(ctx.jokers))
        if not candidates:
            return None
        sell_idx = min(candidates, key=lambda x: x[1].get("cost", {}).get("sell", 99))[0]
        label = ctx.jokers[sell_idx].get("label", "?")
        return SellJoker(sell_idx, reason=f"Verdant Leaf: sell {label} to unlock debuffed cards")


class MilkScalingJokers:
    """If we can already win, play weak hands first to scale jokers."""
    name = "milk_scaling_jokers"

    # Only milk if winning hand covers this much of the blind
    COMFORT_MARGIN = 1.5
    # Keep at least this many hands as safety buffer
    MIN_HANDS_RESERVE = 2

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        ctx = RoundContext.from_state(state)
        if not ctx.best:
            return None

        # Can we already win?
        if ctx.best.total < ctx.chips_remaining:
            return None

        joker_keys = {j.get("key") for j in ctx.jokers}
        has_play_scalers = bool(joker_keys & PLAY_SCALERS)
        has_final_hand = bool(joker_keys & FINAL_HAND_JOKERS)
        has_discard_scalers = bool(joker_keys & DISCARD_SCALERS)

        if not (has_play_scalers or has_final_hand or has_discard_scalers):
            return None

        # Need comfortable margin — don't milk if barely winning
        if ctx.best.total < ctx.chips_remaining * self.COMFORT_MARGIN:
            return None

        # Need spare hands to milk with
        if ctx.hands_left <= self.MIN_HANDS_RESERVE:
            return None

        # Milk via discard first (doesn't cost a hand play)
        if has_discard_scalers and ctx.discards_left > 0:
            # Hit the Road: discard Jacks
            if "j_hit_the_road" in joker_keys:
                for i, c in enumerate(ctx.hand_cards):
                    if card_rank(c) == "J":
                        return DiscardCards(
                            [i], reason=f"milk: discard Jack for Hit the Road scaling",
                        )
            # Castle: discard any card (all contribute)
            if "j_castle" in joker_keys and ctx.hand_cards:
                # Discard lowest-value card
                worst = min(range(len(ctx.hand_cards)),
                           key=lambda i: rank_value(card_rank(ctx.hand_cards[i]) or "2"))
                return DiscardCards(
                    [worst], reason=f"milk: discard for Castle scaling",
                )

        # Milk via weak hand play
        if has_play_scalers or has_final_hand:
            # Avoid face cards if Ride the Bus is in play
            avoid_faces = "j_ride_the_bus" in joker_keys

            # Sort hand cards by rank (weakest first), filtering faces if needed
            playable = []
            for i, c in enumerate(ctx.hand_cards):
                r = card_rank(c)
                if not r:
                    continue
                if avoid_faces and r in FACE_RANKS_SET:
                    continue
                playable.append((i, rank_value(r)))
            playable.sort(key=lambda x: x[1])

            if not playable:
                return None

            scalers = (joker_keys & PLAY_SCALERS) | (joker_keys & FINAL_HAND_JOKERS)

            # Square Joker: play exactly 4 cards to trigger +4 chips scaling
            if "j_square" in joker_keys and len(playable) >= 4:
                milk_indices = [i for i, _ in playable[:4]]
                return PlayCards(
                    milk_indices,
                    reason=f"milk: play 4 cards for Square Joker (+4 chips) ({ctx.hands_left - 1} hands left after)",
                    hand_name="High Card",
                )

            # Default: play single weakest card as High Card
            best_milk_idx = playable[0][0]
            return PlayCards(
                [best_milk_idx],
                reason=f"milk: play weak card to scale {', '.join(sorted(scalers))} ({ctx.hands_left - 1} hands left after)",
                hand_name="High Card",
            )

        return None


class PlayWinningHand:
    """If the best hand beats the remaining blind score, play it."""
    name = "play_winning_hand"

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        ctx = RoundContext.from_state(state)
        if not ctx.best:
            return None
        effective_score = ctx.best.total * ctx.score_discount
        if effective_score >= ctx.chips_remaining:
            indices = _pad_with_junk(ctx.best.card_indices, ctx.hand_cards, ctx.jokers)
            return PlayCards(
                indices,
                reason=f"{ctx.best.hand_name} for {ctx.best.total} (eff {effective_score:.0f}) >= {ctx.chips_remaining} needed",
                hand_name=ctx.best.hand_name,
            )
        return None

class PlayHighValueHand:
    """Play a high-scoring hand even if it won't win, based on hands remaining."""
    name = "play_high_value_hand"

    THRESHOLDS = {
        6: 0.15,
        5: 0.20,
        4: 0.25,
        3: 0.25,
        2: 0.20,
        1: 0.0,   # last hand — play anything, no alternative
    }

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        ctx = RoundContext.from_state(state)
        if not ctx.best:
            return None

        threshold = self.THRESHOLDS.get(ctx.hands_left, 0.15 if ctx.hands_left > 6 else 0.0)
        effective_score = ctx.best.total * ctx.score_discount

        # If the hand meets the threshold, play it — even if it can't solo-clear.
        # A Full House for 60% of the blind is a good play with hands remaining.
        # Only defer to DiscardToImprove when the hand is BELOW the threshold
        # and we have discards to try improving.
        if effective_score < ctx.chips_remaining * threshold and ctx.discards_left > 0:
            return None

        if effective_score >= ctx.chips_remaining * threshold:
            indices = _pad_with_junk(ctx.best.card_indices, ctx.hand_cards, ctx.jokers)
            return PlayCards(
                indices,
                reason=f"{ctx.best.hand_name} for {ctx.best.total} (eff {effective_score:.0f}) >= {threshold:.0%} of {ctx.chips_remaining} remaining",
                hand_name=ctx.best.hand_name,
            )
        return None


class DiscardToImprove:
    """
    If we have discards left and the best hand can't win this turn,
    try to improve by discarding.

    Two modes:
    1. Chase a draw (flush/straight) — always worth it with 2+ discards.
    2. Discard dead cards around current hand — only when the hand is
       hopeless (scores < 30% of what's needed). No point trimming fat
       around a Full House that covers 80% of the blind.
    """
    name = "discard_to_improve"

    # Below this fraction of chips_remaining, the hand is hopeless enough
    # to justify discarding dead cards even without a specific draw.
    # Scales with hands_left: more hands remaining = more tolerant of weak hands.
    HOPELESS_THRESHOLDS = {
        5: 0.15,
        4: 0.20,
        3: 0.25,
        2: 0.35,
    }
    HOPELESS_DEFAULT = 0.50  # 1 hand left: discard if < 50% (you're losing anyway)

    # If best hand covers less than this fraction of chips_remaining,
    # the hand is desperate enough to use the last discard.
    DESPERATE_THRESHOLD = 0.15

    # Jokers that benefit from keeping discards — don't waste discards when these are active
    KEEP_DISCARDS_JOKERS = {
        "j_mystic_summit",  # +15 mult at 0 discards remaining
        "j_banner",         # +30 chips per discard remaining
        "j_delayed_grat",   # $2 per unused discard at end of round
    }

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        ctx = RoundContext.from_state(state)
        if ctx.discards_left <= 0:
            return None
        if not ctx.best:
            return None
        # Only discard if the current best hand can't clear the blind
        if ctx.best.total >= ctx.chips_remaining:
            return None

        # If best hand is already 5 cards (Flush, Straight, Full House, etc.),
        # discarding non-hand cards can't improve it. Only chase draws matter —
        # UNLESS we have extra cards in hand beyond the 5 AND the hand is
        # catastrophically hopeless, in which case discarding the extras cycles
        # dead weight into fresh draws.
        if len(ctx.best.card_indices) >= 5:
            # Still allow chase draws — a better 5-card hand might exist
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
            for indices, reason in suggestions:
                if "chase" in reason:
                    return DiscardCards(indices, reason=reason)

            # Desperation: extra cards in hand + hand covers < 15% of needed.
            # Discard the non-scoring extras to cycle into fresh draws.
            extra_count = len(ctx.hand_cards) - 5
            if extra_count > 0 and ctx.chips_remaining > 0:
                coverage = ctx.best.total / ctx.chips_remaining
                if coverage < 0.15:
                    extras = cards_not_in(ctx.hand_cards, set(ctx.best.card_indices))
                    to_discard = extras[:min(extra_count, ctx.discards_left)]
                    if to_discard:
                        return DiscardCards(
                            to_discard,
                            reason=f"desperation cycle: {ctx.best.hand_name} for {ctx.best.total} is only {coverage:.0%} of {ctx.chips_remaining} needed",
                        )
            return None

        joker_keys = {j.get("key") for j in ctx.jokers}

        # If we have jokers that reward keeping discards, be conservative —
        # BUT only when the hand can actually contribute meaningfully.
        # If best hand can't even cover 20% of what's needed, the discard
        # bonus is irrelevant — we need a better hand or we lose.
        has_keep_discard_jokers = bool(joker_keys & self.KEEP_DISCARDS_JOKERS)
        if has_keep_discard_jokers:
            hand_covers = ctx.best.total / ctx.chips_remaining if ctx.chips_remaining > 0 else 1.0
            if hand_covers >= 0.20:
                # Hand is decent — save discards for the bonus
                return None

        # Build strategy affinity dict for discard weighting
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

        threshold = self.HOPELESS_THRESHOLDS.get(ctx.hands_left, self.HOPELESS_DEFAULT)
        hopeless = ctx.best.total < ctx.chips_remaining * threshold

        # If the hand already covers PlayHighValueHand's play threshold, don't
        # burn discards on weak chases — we can already play this hand. Only
        # accept chases with ≥50% hit probability; below that, just play.
        play_threshold = PlayHighValueHand.THRESHOLDS.get(
            ctx.hands_left, 0.15 if ctx.hands_left > 6 else 0.0
        )
        hand_is_playable = (
            ctx.chips_remaining > 0
            and ctx.best.total >= ctx.chips_remaining * play_threshold
        )

        for indices, reason in suggestions:
            if "chase" in reason:
                if hand_is_playable:
                    # Parse hit probability from reason: "chase X (NN% to hit)"
                    try:
                        chase_prob = int(reason.split("(")[1].split("%")[0]) / 100
                    except (IndexError, ValueError):
                        chase_prob = 1.0
                    if chase_prob < 0.50:
                        continue  # hand is playable — skip weak chase
                return DiscardCards(indices, reason=reason)
            # Discard dead cards when the hand is hopeless AND the
            # best hand uses < 5 cards (otherwise there are no dead cards
            # in the played hand — discarding around a Flush is pointless).
            if hopeless and len(ctx.best.card_indices) < 5:
                return DiscardCards(indices, reason=reason)

        # Last resort: if the hand can't win and we have discards, discard
        # SOMETHING — but only if the best hand uses < 5 cards. A 5-card hand
        # (Flush, Straight, Full House) can't be improved by discarding around it.
        if suggestions and len(ctx.best.card_indices) < 5:
            indices, reason = suggestions[0]
            return DiscardCards(indices, reason=f"discard to improve (below play threshold): {reason}")

        return None


class PlayBestAvailable:
    """Last resort: play the best hand we have, even if it won't clear."""
    name = "play_best_available"

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        ctx = RoundContext.from_state(state)
        # If hand can't win and we still have discards AND the hand is < 5 cards,
        # discard junk to try to improve. 5-card hands can't be improved by discarding.
        if (ctx.best and ctx.best.total < ctx.chips_remaining
                and ctx.discards_left > 0 and len(ctx.best.card_indices) < 5):
            keep = set(ctx.best.card_indices)
            to_discard = cards_not_in(ctx.hand_cards, keep)[:min(5, ctx.discards_left)]
            if to_discard:
                return DiscardCards(to_discard, reason="last resort discard (hand too weak, searching for better)")
        if ctx.best:
            indices = _pad_with_junk(ctx.best.card_indices, ctx.hand_cards, ctx.jokers)
            return PlayCards(
                indices,
                reason=f"best available: {ctx.best.hand_name} for {ctx.best.total}",
                hand_name=ctx.best.hand_name,
            )
        # No valid hand found (e.g. The Mouth locked to a hand type we can't form).
        # Try again without the constraint — playing any 5 cards is better than 1.
        if ctx.mouth_locked_hand and ctx.hand_cards:
            unconstrained = best_hand(
                ctx.hand_cards, ctx.hand_levels,
                min_select=ctx.min_cards, jokers=ctx.jokers,
                money=ctx.money, discards_left=ctx.discards_left,
                hands_left=ctx.hands_left,
            )
            if unconstrained:
                indices = _pad_with_junk(unconstrained.card_indices, ctx.hand_cards, ctx.jokers)
                return PlayCards(
                    indices,
                    reason=f"mouth locked ({ctx.mouth_locked_hand}) but can't form it: "
                           f"playing {unconstrained.hand_name} for {unconstrained.total}",
                    hand_name=unconstrained.hand_name,
                )
        # Absolute fallback: play best 5-card combo we can find
        if ctx.hand_cards:
            if len(ctx.hand_cards) >= 5:
                # Play the 5 highest-value cards
                ranked = sorted(range(len(ctx.hand_cards)),
                                key=lambda i: rank_value(card_rank(ctx.hand_cards[i]) or "2"),
                                reverse=True)
                return PlayCards(ranked[:5], reason="fallback: play 5 highest cards", hand_name="High Card")
            return PlayCards(list(range(len(ctx.hand_cards))), reason="fallback: play all remaining cards", hand_name="High Card")
        return None
