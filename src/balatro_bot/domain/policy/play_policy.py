"""Play-phase policy functions — pure decision logic extracted from rules.

Each function takes a RoundContext, returns an Action or None.
Rules in rules/playing.py become thin wrappers around these.
"""

from __future__ import annotations

import logging
import random
from typing import TYPE_CHECKING

from balatro_bot.actions import PlayCards, DiscardCards, Action
from balatro_bot.cards import card_rank, rank_value, _modifier
from balatro_bot.constants import FACE_RANKS_SET
from balatro_bot.domain.scoring.classify import classify_hand
from balatro_bot.domain.scoring.estimate import score_hand
from balatro_bot.domain.scoring.search import (
    best_hand, cards_not_in, discard_candidates,
    enumerate_hands, ChaseCandidate,
)
from balatro_bot.rules._helpers import _pad_with_junk, _sort_play_order
from balatro_bot.scaling import (
    SCALING_REGISTRY, PLAY_SCALERS, DISCARD_SCALERS,
    FINAL_HAND_JOKERS, SELL_PROTECTED, ANTI_DISCARD, DECAY_JOKERS,
    ScalingProfile,
)

if TYPE_CHECKING:
    from balatro_bot.context import RoundContext

log = logging.getLogger("balatro_bot")


# ---------------------------------------------------------------------------
# PlayWinningHand
# ---------------------------------------------------------------------------

def choose_winning_play(ctx: RoundContext) -> Action | None:
    """If the best hand beats the remaining blind score, play it."""
    if not ctx.best:
        return None
    effective_score = ctx.best.total * ctx.score_discount
    if effective_score >= ctx.chips_remaining:
        indices = _pad_with_junk(ctx.best.card_indices, ctx.hand_cards, ctx.jokers, ctx.best.hand_name)
        indices = _sort_play_order(indices, ctx.hand_cards, ctx.jokers, ctx.strategy)
        return PlayCards(
            indices,
            reason=f"{ctx.best.hand_name} for {ctx.best.total} (eff {effective_score:.0f}) >= {ctx.chips_remaining} needed",
            hand_name=ctx.best.hand_name,
        )
    return None


# ---------------------------------------------------------------------------
# PlayHighValueHand
# ---------------------------------------------------------------------------

# Minimum per-hand contribution thresholds by outlook.
_OUTLOOK_THRESHOLDS = {
    "won": 0.0,
    "comfortable": 0.10,
    "tight": 0.15,
    "hopeless": 0.0,
}

_HAND_SAVE_ANTE_THRESHOLD = 5


def choose_high_value_play(ctx: RoundContext) -> Action | None:
    """Play a high-scoring hand even if it won't win, using round projection.

    Factors in hand-saving economy: each unused hand at round end is worth $1.
    """
    if not ctx.best:
        return None

    # The Needle: only 1 hand play — never play unless it wins
    if ctx.blind_name == "The Needle":
        return None

    outlook = ctx.round_outlook
    effective_score = ctx.best.total * ctx.score_discount

    # Hopeless + discards available -> defer to discard
    if outlook == "hopeless" and ctx.discards_left > 0:
        log.info("PlayHighValueHand: defer (hopeless, %d discards left)", ctx.discards_left)
        return None

    threshold = _OUTLOOK_THRESHOLDS.get(outlook, 0.15)

    # Hand-saving economy: at lower antes when comfortable, raise the bar
    if outlook == "comfortable" and ctx.ante <= _HAND_SAVE_ANTE_THRESHOLD:
        old_threshold = threshold
        threshold = max(threshold, 0.20)
        if threshold > old_threshold:
            log.info("PlayHighValueHand: hand-save bump %.0f%%->%.0f%% (ante %d)", old_threshold * 100, threshold * 100, ctx.ante)

    # Last hand — play anything, no alternative
    if ctx.hands_left <= 1:
        threshold = 0.0

    if ctx.chips_remaining > 0 and effective_score < ctx.chips_remaining * threshold and ctx.discards_left > 0:
        log.info(
            "PlayHighValueHand: defer (%s for %d = %.0f%% of %d, need %.0f%%, outlook=%s)",
            ctx.best.hand_name, ctx.best.total,
            effective_score / ctx.chips_remaining * 100 if ctx.chips_remaining else 0,
            ctx.chips_remaining, threshold * 100, outlook,
        )
        return None

    indices = _pad_with_junk(ctx.best.card_indices, ctx.hand_cards, ctx.jokers, ctx.best.hand_name)
    indices = _sort_play_order(indices, ctx.hand_cards, ctx.jokers, ctx.strategy)
    return PlayCards(
        indices,
        reason=f"{ctx.best.hand_name} for {ctx.best.total} (eff {effective_score:.0f}), outlook={outlook}, {ctx.chips_remaining} remaining",
        hand_name=ctx.best.hand_name,
    )


# ---------------------------------------------------------------------------
# PlayBestAvailable
# ---------------------------------------------------------------------------

def choose_best_available(ctx: RoundContext) -> Action | None:
    """Last resort: play the best hand we have, even if it won't clear."""
    # The Needle: keep discarding if we have discards — don't give up
    if ctx.blind_name == "The Needle" and ctx.discards_left > 0:
        if ctx.best:
            keep = set(ctx.best.card_indices)
            to_discard = cards_not_in(ctx.hand_cards, keep, rank_affinity=ctx.strategy.rank_affinity_dict(), scoring_suit=ctx.scoring_suit)[:min(5, ctx.discards_left)]
            if to_discard:
                return DiscardCards(to_discard, reason="Needle: use all discards to find winning hand")
        return None

    # If hand can't win and we still have discards AND the hand is < 5 cards,
    # discard junk to try to improve.
    if (ctx.best and ctx.best.total < ctx.chips_remaining
            and ctx.discards_left > 0 and len(ctx.best.card_indices) < 5):
        keep = set(ctx.best.card_indices)
        to_discard = cards_not_in(ctx.hand_cards, keep, rank_affinity=ctx.strategy.rank_affinity_dict(), scoring_suit=ctx.scoring_suit)[:min(5, ctx.discards_left)]
        if to_discard:
            return DiscardCards(to_discard, reason="last resort discard (hand too weak, searching for better)")

    if ctx.best:
        indices = _pad_with_junk(ctx.best.card_indices, ctx.hand_cards, ctx.jokers, ctx.best.hand_name)
        indices = _sort_play_order(indices, ctx.hand_cards, ctx.jokers, ctx.strategy)
        return PlayCards(
            indices,
            reason=f"best available: {ctx.best.hand_name} for {ctx.best.total}",
            hand_name=ctx.best.hand_name,
        )

    # No valid hand found (e.g. The Mouth locked to a hand type we can't form).
    if ctx.mouth_locked_hand and ctx.hand_cards:
        unconstrained = best_hand(
            ctx.hand_cards, ctx.hand_levels,
            min_select=ctx.min_cards, jokers=ctx.jokers,
            money=ctx.money, discards_left=ctx.discards_left,
            hands_left=ctx.hands_left,
        )
        if unconstrained:
            indices = _pad_with_junk(unconstrained.card_indices, ctx.hand_cards, ctx.jokers, unconstrained.hand_name)
            indices = _sort_play_order(indices, ctx.hand_cards, ctx.jokers, ctx.strategy)
            return PlayCards(
                indices,
                reason=f"mouth locked ({ctx.mouth_locked_hand}) but can't form it: "
                       f"playing {unconstrained.hand_name} for {unconstrained.total}",
                hand_name=unconstrained.hand_name,
            )

    # Absolute fallback: play best 5-card combo we can find
    if ctx.hand_cards:
        if len(ctx.hand_cards) >= 5:
            ranked = sorted(range(len(ctx.hand_cards)),
                            key=lambda i: rank_value(card_rank(ctx.hand_cards[i]) or "2"),
                            reverse=True)
            indices = _sort_play_order(ranked[:5], ctx.hand_cards, ctx.jokers, ctx.strategy)
            return PlayCards(indices, reason="fallback: play 5 highest cards", hand_name="High Card")
        indices = _sort_play_order(list(range(len(ctx.hand_cards))), ctx.hand_cards, ctx.jokers, ctx.strategy)
        return PlayCards(indices, reason="fallback: play all remaining cards", hand_name="High Card")
    return None


# ---------------------------------------------------------------------------
# MilkScalingJokers
# ---------------------------------------------------------------------------

def choose_milk_play(ctx: RoundContext) -> Action | None:
    """If we can already win, exploit spare hands/discards to scale jokers."""
    effective = ctx.best.total * ctx.score_discount if ctx.best else 0
    if not ctx.best or effective < ctx.chips_remaining:
        return None

    # The Mouth: milking plays a different hand type, which locks us out
    if ctx.mouth_locked_hand is not None or ctx.blind_name == "The Mouth":
        return None

    joker_keys = {j.get("key") for j in ctx.jokers}
    owned_scalers = {k: SCALING_REGISTRY[k] for k in joker_keys if k in SCALING_REGISTRY}
    active = {k: p for k, p in owned_scalers.items() if p.milk_priority > 0}
    if not active:
        return None

    # --- Milk budget ---
    has_final_hand = bool(joker_keys & FINAL_HAND_JOKERS)
    safety = 0 if has_final_hand else 1
    free_hands = ctx.hands_left - 1 - safety
    if free_hands <= 0:
        return None

    # Comfort check
    if ctx.blind_name == "The Wall":
        margin = 2.0
    else:
        margin = 1.25 if free_hands >= 3 else 1.5
    if effective < ctx.chips_remaining * margin:
        return None

    # --- Phase 1: Discard milking ---
    has_banner = "j_banner" in joker_keys
    has_mystic = "j_mystic_summit" in joker_keys
    has_green = "j_green_joker" in joker_keys

    should_preserve_discards = (has_banner and not has_mystic) or has_green

    if not should_preserve_discards and ctx.discards_left > 0:
        discard_action = _milk_discard(ctx, joker_keys, active)
        if discard_action:
            return discard_action

    # --- Phase 2: Mystic Summit — burn all discards ---
    if has_mystic and ctx.discards_left > 0 and not has_green:
        keep = set(ctx.best.card_indices) if ctx.best else set()
        burnable = [
            (i, rank_value(card_rank(ctx.hand_cards[i]) or "2"))
            for i in range(len(ctx.hand_cards)) if i not in keep
        ]
        burnable.sort(key=lambda x: x[1])
        n = min(5, ctx.discards_left, len(burnable))
        if n > 0:
            indices = [i for i, _ in burnable[:n]]
            return DiscardCards(
                indices,
                reason=f"milk: burn {n} for Mystic Summit ({ctx.discards_left} discards left)",
            )

    # --- Phase 3: Play milking ---
    play_scalers = {k: p for k, p in active.items()
                   if p.trigger.startswith("play")}
    final_hand_scalers = {k: p for k, p in active.items()
                         if p.trigger == "final_hand"}

    if play_scalers or final_hand_scalers:
        return _milk_play_action(ctx, joker_keys, play_scalers, final_hand_scalers, free_hands)

    return None


def _milk_discard(ctx: RoundContext, joker_keys: set,
                  active: dict[str, ScalingProfile]) -> Action | None:
    """Try to milk via discards (doesn't cost a hand play)."""
    # Hit the Road: discard Jacks
    if "j_hit_the_road" in active:
        jacks = [i for i, c in enumerate(ctx.hand_cards) if card_rank(c) == "J"]
        if jacks:
            n = min(len(jacks), ctx.discards_left, 5)
            return DiscardCards(
                jacks[:n],
                reason=f"milk: discard {n} Jack(s) for Hit the Road",
            )

    # Castle / Yorick: discard low-value cards
    if any(k in active for k in ("j_castle", "j_yorick")):
        n = min(5, ctx.discards_left, len(ctx.hand_cards))
        if n > 0:
            keep = set(ctx.best.card_indices) if ctx.best else set()
            discardable = [
                (i, rank_value(card_rank(ctx.hand_cards[i]) or "2"))
                for i in range(len(ctx.hand_cards)) if i not in keep
            ]
            discardable.sort(key=lambda x: x[1])
            indices = [i for i, _ in discardable[:n]]
            if indices:
                names = [k for k in ("j_castle", "j_yorick") if k in active]
                return DiscardCards(
                    indices,
                    reason=f"milk: discard {len(indices)} cards for {', '.join(names)}",
                )

    return None


def _milk_play_action(ctx: RoundContext, joker_keys: set,
                      play_scalers: dict, final_hand_scalers: dict,
                      free_hands: int) -> Action | None:
    """Pick the optimal milk hand to play."""
    avoid_faces = "j_ride_the_bus" in joker_keys
    hands_after = ctx.hands_left - 1
    keep = set(ctx.best.card_indices) if ctx.best else set()

    # Build playable card list (weakest first, optionally avoiding faces)
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

    # Junk = playable cards NOT in the winning hand
    junk = [(i, rv) for i, rv in playable if i not in keep]

    # --- Category 1: Card-property triggers ---

    # Square Joker: exactly 4 junk cards
    if "j_square" in play_scalers and len(junk) >= 4:
        indices = _sort_play_order([i for i, _ in junk[:4]], ctx.hand_cards, ctx.jokers, ctx.strategy)
        return PlayCards(
            indices,
            reason=f"milk: cycle 4 junk for Square (+4 chips) ({hands_after} hands left)",
            hand_name=classify_hand([ctx.hand_cards[i] for i in indices]),
        )

    # Vampire: must include an enhanced card, pad with junk
    if "j_vampire" in play_scalers:
        enhanced = [
            (i, rv) for i, rv in junk
            if _modifier(ctx.hand_cards[i]).get("enhancement")
        ]
        if enhanced:
            required = enhanced[0][0]
            filler = [i for i, _ in junk if i != required][:4]
            indices = _sort_play_order([required] + filler, ctx.hand_cards, ctx.jokers, ctx.strategy)
            hand_name = classify_hand([ctx.hand_cards[i] for i in indices])
            return PlayCards(
                indices,
                reason=f"milk: enhanced card + {len(filler)} junk for Vampire (+X0.1) ({hands_after} hands left)",
                hand_name=hand_name,
            )

    # Wee Joker: must include a 2, pad with junk
    if "j_wee" in play_scalers:
        twos = [(i, v) for i, v in junk if v == 2]
        if twos:
            required = twos[0][0]
            filler = [i for i, _ in junk if i != required][:4]
            indices = _sort_play_order([required] + filler, ctx.hand_cards, ctx.jokers, ctx.strategy)
            hand_name = classify_hand([ctx.hand_cards[i] for i in indices])
            return PlayCards(
                indices,
                reason=f"milk: 2 + {len(filler)} junk for Wee (+8 chips) ({hands_after} hands left)",
                hand_name=hand_name,
            )

    # --- Category 2: Hand-type triggers ---
    targets: list[tuple[str, str]] = []

    if "j_runner" in play_scalers:
        targets.extend([("Straight", "j_runner"), ("Straight Flush", "j_runner")])
    if "j_trousers" in play_scalers:
        targets.append(("Two Pair", "j_trousers"))
    if "j_supernova" in play_scalers:
        preferred = ctx.strategy.top_hand()
        if preferred:
            targets.append((preferred, "j_supernova"))
    if "j_hiker" in play_scalers:
        for ht in ("Flush", "Straight", "Full House", "Straight Flush",
                    "Two Pair", "Three of a Kind", "Four of a Kind"):
            targets.append((ht, "j_hiker"))

    if targets:
        candidates = enumerate_hands(
            ctx.hand_cards, ctx.hand_levels,
            jokers=ctx.jokers, joker_limit=len(ctx.jokers),
        )
        for ht, jname in targets:
            match = next((c for c in candidates if c.hand_name == ht), None)
            if match:
                indices = _sort_play_order(match.card_indices, ctx.hand_cards, ctx.jokers, ctx.strategy)
                return PlayCards(
                    indices,
                    reason=f"milk: {ht} for {jname} ({hands_after} hands left)",
                    hand_name=ht,
                )

        # Chase with a discard if affordable
        if ctx.discards_left > 0 and free_hands >= 2:
            for ht, jname in targets:
                chases = discard_candidates(
                    ctx.hand_cards, ctx.hand_levels,
                    jokers=ctx.jokers, required_hand=ht,
                    deck_cards=ctx.deck_cards,
                )
                if chases:
                    indices, reason = chases[0]
                    return DiscardCards(
                        indices,
                        reason=f"milk: chase {ht} for {jname} ({reason})",
                    )

    # --- Category 3: Generic triggers ---
    generic = {k for k, p in play_scalers.items()
               if p.trigger in ("play", "play_no_face")}
    generic |= set(final_hand_scalers)
    if "j_supernova" in play_scalers:
        generic.add("j_supernova")
    if "j_hiker" in play_scalers:
        generic.add("j_hiker")

    if generic:
        dump = junk[:5] if junk else playable[:1]
        indices = _sort_play_order([i for i, _ in dump], ctx.hand_cards, ctx.jokers, ctx.strategy)
        hand_name = classify_hand([ctx.hand_cards[i] for i in indices])
        return PlayCards(
            indices,
            reason=f"milk: cycle {len(indices)} junk for {', '.join(sorted(generic))} ({hands_after} hands left)",
            hand_name=hand_name,
        )

    return None
