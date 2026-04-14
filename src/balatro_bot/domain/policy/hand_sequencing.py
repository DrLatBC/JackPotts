"""Hand sequencing — round-level play ordering for maximum scoring.

Instead of greedily playing the best hand every turn, this module plans
a sequence across the round: milk early, set up Card Sharp, and save
the best hand for the Acrobat/Dusk finisher on the final hand.

The plan is rebuilt every tick (cheap) and is advisory — if a step
can't execute, it returns None and the existing greedy rules take over.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from balatro_bot.actions import Action, PlayCards, DiscardCards
from balatro_bot.cards import card_rank, joker_key, rank_value
from balatro_bot.constants import FACE_RANKS_SET
from balatro_bot.domain.scoring.classify import classify_hand
from balatro_bot.domain.scoring.search import (
    enumerate_hands, best_hand, cards_not_in, HandCandidate,
)
from balatro_bot.domain.policy.play_policy import (
    milk_discard, milk_play_action,
    _MILK_MARGIN_WALL, _MILK_MARGIN_MANY, _MILK_MARGIN_FEW,
)
from balatro_bot.rules._helpers import _pad_with_junk, _sort_play_order
from balatro_bot.scaling import (
    SCALING_REGISTRY, FINAL_HAND_JOKERS, SEQUENCE_JOKERS,
    PLAY_SCALERS, DISCARD_SCALERS, ANTI_DISCARD, ANTI_MILK,
    FIRST_HAND_JOKERS, FIRST_DISCARD_JOKERS,
    DECAY_JOKERS, ScalingProfile,
)

if TYPE_CHECKING:
    from balatro_bot.context import RoundContext

log = logging.getLogger("balatro_bot")


# ---------------------------------------------------------------------------
# Plan data structures
# ---------------------------------------------------------------------------

@dataclass
class PlanStep:
    intent: str  # "milk", "setup", "finisher", "score"
    target_type: str | None = None  # hand type for setup/finisher
    score_estimate: int = 0


@dataclass
class RoundPlan:
    steps: list[PlanStep]
    finisher_score: int = 0
    card_sharp_type: str | None = None
    total_hands: int = 0  # hands_left when the plan was built


# ---------------------------------------------------------------------------
# Safety margin constants
# ---------------------------------------------------------------------------

# How much projected total must exceed chips_remaining to allow milking.
# Lower than the old milking margins because Acrobat's x3 gives real payoff.
_PLAN_MARGIN_COMFORTABLE = 1.15
_PLAN_MARGIN_TIGHT = 1.0  # at tight, allow if we project to just clear
_PLAN_MARGIN_WALL = 1.8

# Bosses where milking is forbidden — the cost of junk plays is too high.
# The Mouth: locks hand type on first play, junk = locked into High Card.
# The Flint: halves base chips+mult, can't afford to waste any hand.
# The Eye: no repeat hand types — milking burns through types needed for scoring.
_NO_MILK_BOSSES = {"The Mouth", "The Flint", "The Eye"}


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------

def build_round_plan(ctx: RoundContext) -> RoundPlan | None:
    """Build a round-level hand-ordering plan.

    Returns None when no sequencing is beneficial (no relevant jokers,
    only 1 hand left, boss constraints, etc.).
    """
    if ctx.hands_left <= 1:
        return None

    # Boss constraints that prevent sequencing
    if ctx.blind_name == "The Needle":
        return None

    joker_keys = {joker_key(j) for j in ctx.jokers}

    # --- The Eye: sequence hand types weakest-first, strongest-last ---
    if ctx.blind_name == "The Eye" and ctx.hands_left >= 2:
        return _build_eye_plan(ctx)

    has_final_hand = bool(joker_keys & FINAL_HAND_JOKERS)
    has_card_sharp = bool(joker_keys & SEQUENCE_JOKERS)
    has_loyalty = "j_loyalty_card" in joker_keys
    loyalty_fires_in = _loyalty_fires_in(ctx.jokers) if has_loyalty else None
    owned_scalers = {k: SCALING_REGISTRY[k] for k in joker_keys if k in SCALING_REGISTRY}
    active_scalers = {k: p for k, p in owned_scalers.items() if p.milk_priority > 0}

    # No sequencing-relevant jokers at all
    if not has_final_hand and not has_card_sharp and not has_loyalty and not active_scalers:
        return None

    # The Mouth: can't vary hand types, limits sequencing.
    # Still allow finisher optimization (save best for last).
    mouth_active = ctx.mouth_locked_hand is not None or ctx.blind_name == "The Mouth"

    # --- Phase 1: Final-hand projection ---
    finisher = ctx.best_as_finisher  # pre-computed in context.py
    finisher_score = finisher.total * ctx.score_discount if finisher else 0
    best_now_score = ctx.best.total * ctx.score_discount if ctx.best else 0

    # Is saving for the finisher worth it?
    # Acrobat gives x3, Dusk roughly doubles — so finisher should be noticeably better.
    reserve_finisher = has_final_hand and finisher_score > best_now_score * 1.2

    # --- Phase 2: Card Sharp sequencing ---
    card_sharp_type = None
    card_sharp_steps = 0
    if has_card_sharp and not mouth_active and ctx.hands_left >= 3:
        card_sharp_type = _find_card_sharp_type(ctx)
        if card_sharp_type:
            card_sharp_steps = 1  # 1 setup step (the score step is the 2nd play)

    # --- Phase 3: Milk budget ---
    finisher_reserved = 1 if reserve_finisher else 0
    if ctx.blind_name in _NO_MILK_BOSSES:
        milk_budget = 0
        log.info("RoundPlan: %s — milking disabled", ctx.blind_name)
    else:
        milk_budget = ctx.hands_left - 1 - finisher_reserved - card_sharp_steps
    if milk_budget < 0:
        milk_budget = 0

    # --- Phase 3b: Anti-milk constraints ---
    # Selzer: retrigger all cards for N hands then self-destructs.
    # Each hand spent on junk wastes a precious retrigger.  Cut milk budget
    # when Selzer is active (remaining uses > 0).
    selzer_remaining = _selzer_remaining(ctx.jokers) if "j_selzer" in joker_keys else None
    if selzer_remaining is not None and selzer_remaining > 0:
        # Don't milk while Selzer is alive — every hand gets retriggers
        old_budget = milk_budget
        milk_budget = max(0, milk_budget - selzer_remaining)
        if milk_budget < old_budget:
            log.info("RoundPlan: Selzer active (%d left), cut milk %d -> %d",
                     selzer_remaining, old_budget, milk_budget)

    # Glass Joker: 1/4 chance to destroy glass cards each play.
    # More plays = more risk.  Reduce milk budget when glass cards are in hand.
    if "j_glass" in joker_keys:
        glass_count = sum(
            1 for c in ctx.hand_cards
            if (c.modifier.enhancement if hasattr(c, 'modifier') else None) == "GLASS"
        )
        if glass_count > 0:
            old_budget = milk_budget
            # Each milk play has ~25% * glass_count chance of destroying a card.
            # Cap milk at 1 if glass cards present — don't risk them.
            milk_budget = min(milk_budget, 1)
            if milk_budget < old_budget:
                log.info("RoundPlan: Glass Joker + %d glass cards, cut milk %d -> %d",
                         glass_count, old_budget, milk_budget)

    # --- Phase 4: Safety gate ---
    # Project total output: milk hands contribute ~avg score, finisher at projected score.
    avg_hand_score = best_now_score  # rough estimate for non-finisher hands
    if card_sharp_type and card_sharp_steps > 0:
        # Card Sharp setup + score: first play = base, second play = base * 3
        # This is spread across setup step (base) + the next hand (base * 3)
        cs_bonus = avg_hand_score * 2  # extra from x3 on the second play
    else:
        cs_bonus = 0

    projected_total = (
        milk_budget * avg_hand_score * 0.3  # milk hands score much less (junk plays)
        + (1 if not reserve_finisher else 0) * avg_hand_score  # regular score hand
        + cs_bonus
        + finisher_reserved * finisher_score
    )

    if ctx.chips_remaining > 0 and projected_total > 0:
        margin = _plan_safety_margin(ctx)
        if projected_total < ctx.chips_remaining * margin:
            # Not safe enough to milk — reduce budget
            if reserve_finisher and finisher_score >= ctx.chips_remaining:
                # Finisher alone can win — milk freely (just waste fewer hands)
                pass
            else:
                # Trim milk budget until we project enough
                while milk_budget > 0:
                    milk_budget -= 1
                    projected_total = (
                        milk_budget * avg_hand_score * 0.3
                        + max(1, ctx.hands_left - 1 - finisher_reserved - milk_budget - card_sharp_steps) * avg_hand_score
                        + cs_bonus
                        + finisher_reserved * finisher_score
                    )
                    if projected_total >= ctx.chips_remaining * margin:
                        break

    # --- Phase 5: Build step list ---
    # Determine which step indices are "power hands" where Loyalty Card fires.
    # loyalty_fires_in == 0 means fires THIS hand (step 0),
    # loyalty_fires_in == 2 means fires in 2 hands (step 2), etc.
    # Loyalty cycles every 4 hands (configurable), so it may fire multiple times.
    loyalty_power_steps: set[int] = set()
    if loyalty_fires_in is not None and loyalty_fires_in < ctx.hands_left:
        loyalty_cycle = _loyalty_cycle(ctx.jokers)
        step = loyalty_fires_in
        while step < ctx.hands_left:
            loyalty_power_steps.add(step)
            step += loyalty_cycle

    steps: list[PlanStep] = []
    remaining = ctx.hands_left

    # Milk steps first — but promote to "score" if Loyalty fires on that hand
    for i in range(milk_budget):
        step_idx = i  # milk steps are first
        if step_idx in loyalty_power_steps:
            steps.append(PlanStep("score", score_estimate=int(avg_hand_score * 4)))
            log.info("RoundPlan: promoting milk step %d to score (Loyalty Card fires)", step_idx)
        else:
            steps.append(PlanStep("milk", score_estimate=int(avg_hand_score * 0.3)))
        remaining -= 1

    # Card Sharp setup step (play target type once to prime it)
    if card_sharp_type and card_sharp_steps > 0 and remaining >= 2 + finisher_reserved:
        steps.append(PlanStep("setup", target_type=card_sharp_type,
                              score_estimate=int(avg_hand_score)))
        remaining -= 1
        # The next "score" step benefits from Card Sharp x3
        steps.append(PlanStep("score", target_type=card_sharp_type,
                              score_estimate=int(avg_hand_score * 3)))
        remaining -= 1

    # Fill remaining non-finisher hands as score steps
    while remaining > (1 if reserve_finisher else 0):
        steps.append(PlanStep("score", score_estimate=int(avg_hand_score)))
        remaining -= 1

    # Finisher step (or final score step if no finisher)
    if reserve_finisher and remaining >= 1:
        steps.append(PlanStep("finisher", score_estimate=int(finisher_score)))
    elif remaining >= 1:
        steps.append(PlanStep("score", score_estimate=int(avg_hand_score)))

    if not steps:
        return None

    plan = RoundPlan(
        steps=steps,
        finisher_score=int(finisher_score),
        card_sharp_type=card_sharp_type,
        total_hands=ctx.hands_left,
    )

    loyalty_str = f" | loyalty_fires_in={loyalty_fires_in}" if loyalty_fires_in is not None else ""
    log.info(
        "RoundPlan: %s | finisher=%s (%d) | card_sharp=%s%s | chips_remaining=%d",
        " -> ".join(f"{s.intent}({s.target_type or ''})" for s in steps),
        "yes" if reserve_finisher else "no",
        int(finisher_score),
        card_sharp_type or "none",
        loyalty_str,
        ctx.chips_remaining,
    )

    return plan


# ---------------------------------------------------------------------------
# The Eye planner
# ---------------------------------------------------------------------------

def _build_eye_plan(ctx: RoundContext) -> RoundPlan | None:
    """Build a plan for The Eye: play weakest hand types first, strongest last.

    The Eye eliminates each hand type after it's played.  Greedy play wastes
    the strongest type first, leaving only weak options.  By reversing the
    order we save the high-scoring type for when we need it most.
    """
    eye_used = ctx.eye_used_hands or set()

    # Enumerate all distinct hand types we can form right now
    candidates = enumerate_hands(
        ctx.hand_cards, ctx.hand_levels,
        jokers=ctx.jokers, joker_limit=len(ctx.jokers),
        hands_left=ctx.hands_left,
        excluded_hands=eye_used,
    )
    if not candidates:
        return None

    # Deduplicate: keep the best candidate per hand type
    best_per_type: dict[str, HandCandidate] = {}
    for c in candidates:
        if c.hand_name not in best_per_type or c.total > best_per_type[c.hand_name].total:
            best_per_type[c.hand_name] = c

    # Sort weakest first — play cheap types early, save the strongest for last
    sorted_types = sorted(best_per_type.items(), key=lambda kv: kv[1].total)

    # Build steps: one "score" step per hand type, weakest first
    steps: list[PlanStep] = []
    for hand_type, cand in sorted_types[:ctx.hands_left]:
        steps.append(PlanStep(
            "score",
            target_type=hand_type,
            score_estimate=int(cand.total * ctx.score_discount),
        ))

    if not steps:
        return None

    plan = RoundPlan(
        steps=steps,
        finisher_score=int(sorted_types[-1][1].total * ctx.score_discount) if sorted_types else 0,
        total_hands=ctx.hands_left,
    )

    type_seq = " -> ".join(f"{s.target_type}({s.score_estimate})" for s in steps)
    log.info("RoundPlan[Eye]: %s | %d types available, %d hands, used=%s",
             type_seq, len(best_per_type), ctx.hands_left, eye_used or "{}")

    return plan


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------

def execute_plan_step(plan: RoundPlan, ctx: RoundContext) -> Action | None:
    """Execute the current step of the round plan.

    Returns None if the step can't execute (falls through to greedy rules).
    """
    # --- First-action-of-round optimizations ---
    # These fire exactly once per round, before normal step dispatch.
    is_first_action = _is_first_action_of_round(ctx)
    if is_first_action:
        first_action = _first_round_action(ctx)
        if first_action:
            return first_action

    step_index = plan.total_hands - ctx.hands_left
    if step_index < 0 or step_index >= len(plan.steps):
        return None

    step = plan.steps[step_index]

    if step.intent == "finisher":
        return _execute_finisher(ctx)
    elif step.intent == "setup":
        return _execute_setup(ctx, step.target_type)
    elif step.intent == "score":
        return _execute_score(ctx, step.target_type)
    elif step.intent == "milk":
        return _execute_milk(ctx)
    return None


def _execute_finisher(ctx: RoundContext) -> Action | None:
    """Play the best hand — Acrobat/Dusk will fire since this is hands_left=1."""
    # On the actual final hand, ctx.best already includes Acrobat/Dusk scoring
    # because hands_left==1 at this point. But use best_as_finisher if we
    # pre-computed it (it may pick a different hand that's better as finisher).
    candidate = ctx.best_as_finisher or ctx.best
    if not candidate:
        return None

    indices = _pad_with_junk(
        candidate.card_indices, ctx.hand_cards, ctx.jokers,
        candidate.hand_name, strategy=ctx.strategy, scoring_suit=ctx.scoring_suit,
    )
    indices = _sort_play_order(indices, ctx.hand_cards, ctx.jokers, ctx.strategy)

    effective = candidate.total * ctx.score_discount
    return PlayCards(
        indices,
        reason=f"finisher: {candidate.hand_name} for {candidate.total} "
               f"(eff {effective:.0f}), {ctx.chips_remaining} remaining",
        hand_name=candidate.hand_name,
        total=candidate.total,
    )


def _execute_setup(ctx: RoundContext, target_type: str | None) -> Action | None:
    """Play the target hand type to set up Card Sharp for the next hand."""
    if not target_type:
        return None

    # Find the target hand type in current hand
    candidates = enumerate_hands(
        ctx.hand_cards, ctx.hand_levels,
        jokers=ctx.jokers, joker_limit=len(ctx.jokers),
        hands_left=ctx.hands_left,
        excluded_hands=ctx.eye_used_hands,
    )
    match = next((c for c in candidates if c.hand_name == target_type), None)

    if match:
        indices = _pad_with_junk(
            match.card_indices, ctx.hand_cards, ctx.jokers,
            match.hand_name, strategy=ctx.strategy, scoring_suit=ctx.scoring_suit,
        )
        indices = _sort_play_order(indices, ctx.hand_cards, ctx.jokers, ctx.strategy)
        return PlayCards(
            indices,
            reason=f"setup: {target_type} for Card Sharp (next same-type gets x3)",
            hand_name=target_type,
            total=match.total,
        )

    # Can't form target type — try discarding to chase it
    if ctx.discards_left > 0:
        from balatro_bot.domain.scoring.search import discard_candidates
        chases = discard_candidates(
            ctx.hand_cards, ctx.hand_levels,
            jokers=ctx.jokers, required_hand=target_type,
            deck_cards=ctx.deck_cards,
        )
        if chases:
            best_chase = chases[0]
            return DiscardCards(
                best_chase.discard_indices,
                reason=f"setup chase: discard to find {target_type} for Card Sharp",
            )

    return None


def _execute_score(ctx: RoundContext, target_type: str | None = None) -> Action | None:
    """Play a scoring hand — optionally targeting a specific type for Card Sharp."""
    candidate = ctx.best
    if not candidate:
        return None

    # If we have a target type (Card Sharp second play), prefer it
    if target_type:
        candidates = enumerate_hands(
            ctx.hand_cards, ctx.hand_levels,
            jokers=ctx.jokers, joker_limit=len(ctx.jokers),
            hands_left=ctx.hands_left,
            excluded_hands=ctx.eye_used_hands,
        )
        type_match = next((c for c in candidates if c.hand_name == target_type), None)
        if type_match:
            candidate = type_match

    indices = _pad_with_junk(
        candidate.card_indices, ctx.hand_cards, ctx.jokers,
        candidate.hand_name, strategy=ctx.strategy, scoring_suit=ctx.scoring_suit,
    )
    indices = _sort_play_order(indices, ctx.hand_cards, ctx.jokers, ctx.strategy)

    effective = candidate.total * ctx.score_discount
    if target_type and ctx.blind_name == "The Eye":
        suffix = f" (Eye sequence: targeting {target_type})"
    elif target_type:
        suffix = f" (targeting {target_type} for Card Sharp)"
    else:
        suffix = ""
    return PlayCards(
        indices,
        reason=f"score: {candidate.hand_name} for {candidate.total} "
               f"(eff {effective:.0f}){suffix}, {ctx.chips_remaining} remaining",
        hand_name=candidate.hand_name,
        total=candidate.total,
    )


def _execute_milk(ctx: RoundContext) -> Action | None:
    """Execute a milking step — delegate to the existing milk helpers."""
    joker_keys = {joker_key(j) for j in ctx.jokers}
    owned_scalers = {k: SCALING_REGISTRY[k] for k in joker_keys if k in SCALING_REGISTRY}
    active = {k: p for k, p in owned_scalers.items() if p.milk_priority > 0}

    if not active:
        # No scalers to milk — play a junk hand to burn the hand slot
        return _play_junk(ctx)

    # --- Discard milking (doesn't cost a hand play) ---
    has_banner = "j_banner" in joker_keys
    has_mystic = "j_mystic_summit" in joker_keys
    has_green = "j_green_joker" in joker_keys

    if has_green and ctx.blind_name == "The Hook":
        active.pop("j_green_joker", None)

    should_preserve_discards = (has_banner and not has_mystic) or has_green

    if not should_preserve_discards and ctx.discards_left > 0:
        discard_action = milk_discard(ctx, joker_keys, active)
        if discard_action:
            return discard_action

    # --- Mystic Summit: burn discards ---
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
                reason=f"plan milk: burn {n} for Mystic Summit ({ctx.discards_left} discards left)",
            )

    # --- Play milking ---
    play_scalers = {k: p for k, p in active.items() if p.trigger.startswith("play")}
    final_hand_scalers = {k: p for k, p in active.items() if p.trigger == "final_hand"}

    if play_scalers or final_hand_scalers:
        # Use existing milk play logic — pass current hands_left as free_hands
        # since the plan already accounts for the milk budget
        action = milk_play_action(ctx, joker_keys, play_scalers, final_hand_scalers,
                                  free_hands=ctx.hands_left - 1)
        if action:
            return action

    # --- To-Do List: play target hand type for $4 during milk ---
    if "j_todo_list" in joker_keys:
        action = _todo_list_milk(ctx)
        if action:
            return action

    # Fallback: play junk to burn the hand slot for scaling
    return _play_junk(ctx)


def _play_junk(ctx: RoundContext) -> Action | None:
    """Play the weakest possible hand as a throwaway."""
    keep = set(ctx.best.card_indices) if ctx.best else set()
    avoid_faces = any(joker_key(j) == "j_ride_the_bus" for j in ctx.jokers)
    eye_used = ctx.eye_used_hands or set()

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

    # Junk = cards not in winning hand
    junk = [(i, rv) for i, rv in playable if i not in keep]
    dump = junk[:5] if junk else playable[:1]

    if not dump:
        return None

    indices = [i for i, _ in dump]
    hand_name = classify_hand([ctx.hand_cards[i] for i in indices])

    # The Eye: don't play a hand type that's already been used
    if hand_name in eye_used:
        # Try smaller subsets to get a different type
        for size in range(len(dump) - 1, 0, -1):
            sub = dump[:size]
            sub_indices = [i for i, _ in sub]
            sub_type = classify_hand([ctx.hand_cards[i] for i in sub_indices])
            if sub_type not in eye_used:
                indices = sub_indices
                hand_name = sub_type
                break
        else:
            # All subsets produce used types — skip junk play
            return None

    indices = _sort_play_order(indices, ctx.hand_cards, ctx.jokers,
                               ctx.strategy if hasattr(ctx, 'strategy') else None)
    return PlayCards(
        indices,
        reason=f"plan milk: cycle {len(indices)} junk ({ctx.hands_left - 1} hands left)",
        hand_name=hand_name,
    )


def _todo_list_hand_type(jokers: list) -> str | None:
    """Return the hand type that To-Do List rewards, or None."""
    for j in jokers:
        if joker_key(j) == "j_todo_list":
            ab = j.value.ability
            return ab.to_do_poker_hand or ab.poker_hand
    return None


def _todo_list_milk(ctx: RoundContext) -> Action | None:
    """During a milk step, try to play the To-Do List target type for $4.

    Attempts to form the target from non-best cards. If it can't be formed
    from junk alone, uses enumerate_hands to find any combo that matches.
    """
    target = _todo_list_hand_type(ctx.jokers)
    if not target:
        return None

    # First: try to form it from junk cards (preserve best hand)
    keep = set(ctx.best.card_indices) if ctx.best else set()
    junk_indices = [i for i in range(len(ctx.hand_cards)) if i not in keep]

    if junk_indices:
        from itertools import combinations
        junk_cards = [(i, ctx.hand_cards[i]) for i in junk_indices]
        for size in range(min(5, len(junk_cards)), 0, -1):
            for combo in combinations(junk_cards, size):
                indices = [i for i, _ in combo]
                cards = [c for _, c in combo]
                if classify_hand(cards) == target:
                    indices = _sort_play_order(
                        indices, ctx.hand_cards, ctx.jokers,
                        ctx.strategy if hasattr(ctx, 'strategy') else None,
                    )
                    log.info("To-Do List: playing %s from junk for $4", target)
                    return PlayCards(
                        indices,
                        reason=f"plan milk: To-Do List {target} for $4 "
                               f"({ctx.hands_left - 1} hands left)",
                        hand_name=target,
                    )

    # Second: search all combos — may overlap with best hand but still worth $4
    candidates = enumerate_hands(
        ctx.hand_cards, ctx.hand_levels,
        jokers=ctx.jokers, joker_limit=len(ctx.jokers),
        hands_left=ctx.hands_left,
    )
    match = next((c for c in candidates if c.hand_name == target), None)
    if match:
        indices = _sort_play_order(
            list(match.card_indices), ctx.hand_cards, ctx.jokers,
            ctx.strategy if hasattr(ctx, 'strategy') else None,
        )
        log.info("To-Do List: playing %s (any cards) for $4", target)
        return PlayCards(
            indices,
            reason=f"plan milk: To-Do List {target} for $4 "
                   f"({ctx.hands_left - 1} hands left)",
            hand_name=target,
        )

    return None


# ---------------------------------------------------------------------------
# First-action-of-round logic
# ---------------------------------------------------------------------------

def _is_first_action_of_round(ctx: RoundContext) -> bool:
    """Check if no hands have been played and no discards used this round.

    Uses played_this_round counters on hand levels as a proxy — if all are 0,
    no hands have been played yet.
    """
    for _ht, data in ctx.hand_levels.items():
        if hasattr(data, "get") and data.get("played_this_round", 0) > 0:
            return False
    return True


def _first_round_action(ctx: RoundContext) -> Action | None:
    """One-shot optimizations for the very first action of a round.

    Handles:
    - Burnt Joker: discard cards that form the strategy hand type for a free level-up
    - Sixth Sense: include a 6 in the first hand for Spectral generation
    """
    joker_keys = {joker_key(j) for j in ctx.jokers}

    # --- First-discard optimizations ---
    has_burnt = "j_burnt" in joker_keys
    has_trading = "j_trading" in joker_keys

    if (has_burnt or has_trading) and ctx.discards_left > 0:
        # Burnt Joker: try to discard the strategy hand type for a free level-up
        if has_burnt:
            action = _burnt_first_discard(ctx)
            if action:
                return action

        # Trading Card: $3 for any first discard.
        # If Burnt couldn't form the strategy type, still discard junk for $3.
        if has_trading:
            action = _trading_first_discard(ctx)
            if action:
                return action

    # --- Sixth Sense / DNA: solo plays that lock hand type ---
    # Skip against The Mouth — a solo High Card locks you into the worst type.
    if ctx.blind_name not in _NO_MILK_BOSSES:
        if "j_sixth_sense" in joker_keys:
            action = _sixth_sense_solo(ctx)
            if action:
                return action

        if "j_dna" in joker_keys:
            action = _dna_solo(ctx)
            if action:
                return action

    return None


def _burnt_first_discard(ctx: RoundContext) -> Action | None:
    """Burnt Joker: on first discard, discard cards that form the strategy
    hand type to level it up for free.

    Only fires when it's the first action of the round and discards are available.
    """
    from balatro_bot.domain.scoring.classify import classify_hand as _classify

    preferred = ctx.strategy.top_hand()
    if not preferred:
        return None

    # Find a subset of cards that classifies as the preferred hand type.
    # We want to discard UP TO 5 cards that form this hand type.
    # Try subsets from the non-best-hand cards first.
    keep_best = set(ctx.best.card_indices) if ctx.best else set()
    discardable = [i for i in range(len(ctx.hand_cards)) if i not in keep_best]

    if not discardable:
        return None

    # Try subsets of discardable cards to find one that classifies as preferred type
    from itertools import combinations
    for size in range(min(5, len(discardable)), 0, -1):
        for combo in combinations(discardable, size):
            cards = [ctx.hand_cards[i] for i in combo]
            hand_type = _classify(cards)
            if hand_type == preferred:
                log.info("Burnt Joker: discarding %d cards as %s for free level-up",
                         size, preferred)
                return DiscardCards(
                    list(combo),
                    reason=f"Burnt Joker: discard {preferred} for free level-up",
                )

    # Couldn't form preferred type from discardable cards — skip
    return None


def _trading_first_discard(ctx: RoundContext) -> Action | None:
    """Trading Card: discard exactly 1 junk card for $3.

    Fires on the first discard of the round. Only when comfortable —
    spending a discard is cheap but not free.
    """
    if ctx.round_outlook not in ("comfortable", "won"):
        return None

    keep = set(ctx.best.card_indices) if ctx.best else set()
    discardable = [
        (i, rank_value(card_rank(ctx.hand_cards[i]) or "2"))
        for i in range(len(ctx.hand_cards)) if i not in keep
    ]
    if not discardable:
        return None

    discardable.sort(key=lambda x: x[1])
    idx = discardable[0][0]
    log.info("Trading Card: discarding 1 junk card for $3")
    return DiscardCards(
        [idx],
        reason="Trading Card: discard 1 junk for $3",
    )


def _is_comfortable_without_hand(ctx: RoundContext, margin: float = 1.1) -> bool:
    """Can the bot still win the round if it spends one hand on a non-scoring play?

    Used as a safety gate for Sixth Sense / DNA solo plays.
    Requires that the remaining hands (minus this one) can cover chips_remaining.

    The margin parameter lets callers tune aggressiveness — Sixth Sense (Spectral
    reward) uses a lower bar than DNA (card duplication reward).
    """
    if ctx.chips_remaining <= 0:
        return True
    effective = ctx.best.total * ctx.score_discount if ctx.best else 0
    if effective <= 0:
        return False
    remaining_hands = ctx.hands_left - 1  # minus the hand we're spending
    projected = effective * remaining_hands
    return projected >= ctx.chips_remaining * margin


def _sixth_sense_solo(ctx: RoundContext) -> Action | None:
    """Sixth Sense: play a solo 6 as the very first hand of the round.

    Requirements (from game source):
    - Exactly 1 card played
    - That card is a 6
    - It's the first hand of the round (hands_played == 0)

    Reward: destroys the 6 and creates a Spectral card. Spectral cards are
    among the most powerful consumables — deck thinning, enhancements, etc.

    Only fires when comfortable (can win without this hand).
    Uses a low 0.95x margin because Spectral cards are extremely high value —
    worth a slight risk to the current round for massive run-level payoff.
    """
    if ctx.hands_left <= 1:
        return None  # can't afford to spend the only hand

    if not _is_comfortable_without_hand(ctx, margin=0.95):
        return None

    # Find a 6 in hand
    six_idx = None
    for i, c in enumerate(ctx.hand_cards):
        r = card_rank(c)
        if r == "6":
            six_idx = i
            break

    if six_idx is None:
        return None

    log.info("Sixth Sense: playing solo 6 (idx %d) for free Spectral card "
             "(%d hands left, %d chips remaining)",
             six_idx, ctx.hands_left, ctx.chips_remaining)
    return PlayCards(
        [six_idx],
        reason=f"Sixth Sense: solo 6 for free Spectral "
               f"({ctx.hands_left - 1} hands remain, {ctx.chips_remaining} to go)",
        hand_name="High Card",
    )


def _dna_solo(ctx: RoundContext) -> Action | None:
    """DNA Joker: play a solo card as the very first hand of the round.

    Requirements (from game source):
    - Exactly 1 card played
    - It's the first hand of the round (hands_played == 0)

    Reward: creates a permanent copy of the played card in the deck.
    Best with high-value or enhanced cards — duplicating a strong card
    makes future hands better.

    Only fires when comfortable (can win without this hand).
    Uses 1.1x margin — card duplication is good but lower value than Spectral.
    """
    if ctx.hands_left <= 1:
        return None

    if not _is_comfortable_without_hand(ctx, margin=1.1):
        return None

    # Pick the best solo card to duplicate.
    # Priorities: enhanced cards > high rank > low rank
    # Avoid face cards if Ride the Bus is owned (resets its counter).
    joker_keys = {joker_key(j) for j in ctx.jokers}
    avoid_faces = "j_ride_the_bus" in joker_keys

    best_idx = None
    best_score = -1

    for i, c in enumerate(ctx.hand_cards):
        r = card_rank(c)
        if not r:
            continue
        if avoid_faces and r in FACE_RANKS_SET:
            continue

        score = rank_value(r)

        # Prefer enhanced cards — duplicating an enhanced card is very strong
        enhancement = c.modifier.enhancement if hasattr(c, 'modifier') else None
        if enhancement and enhancement != "NONE":
            score += 100  # heavily prefer enhanced

        # Prefer cards with seals
        seal = c.modifier.seal if hasattr(c, 'modifier') else None
        if seal and seal != "NONE":
            score += 50

        # Prefer cards matching strategy suit
        if ctx.scoring_suit:
            from balatro_bot.cards import card_suit
            if card_suit(c) == ctx.scoring_suit:
                score += 20

        if score > best_score:
            best_score = score
            best_idx = i

    if best_idx is None:
        return None

    card = ctx.hand_cards[best_idx]
    r = card_rank(card)
    log.info("DNA: playing solo %s (idx %d) to duplicate into deck "
             "(%d hands left, %d chips remaining)",
             r, best_idx, ctx.hands_left, ctx.chips_remaining)
    return PlayCards(
        [best_idx],
        reason=f"DNA: solo {r} to duplicate into deck "
               f"({ctx.hands_left - 1} hands remain, {ctx.chips_remaining} to go)",
        hand_name="High Card",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_card_sharp_type(ctx: RoundContext) -> str | None:
    """Find the hand type that maximizes a Card Sharp [T, T] sequence.

    Card Sharp gives x3 mult when you play the same hand type as the
    previous play this round. We want the type T where
    score(T) + score(T)*3 is maximized.

    Also considers types already played this round — if we've already
    played a Two Pair, playing another Two Pair immediately gets x3.
    """
    candidates = enumerate_hands(
        ctx.hand_cards, ctx.hand_levels,
        jokers=ctx.jokers, joker_limit=len(ctx.jokers),
        hands_left=ctx.hands_left,
    )

    if not candidates:
        return None

    # Group by hand type — only consider types we can actually form
    by_type: dict[str, HandCandidate] = {}
    for c in candidates:
        if c.hand_name not in by_type:
            by_type[c.hand_name] = c

    # Check if any type was already played this round (Card Sharp already primed)
    already_played_types = set()
    for ht, data in ctx.hand_levels.items():
        if hasattr(data, "get") and data.get("played_this_round", 0) > 0:
            already_played_types.add(ht)

    best_pair_score = 0
    best_type = None

    for hand_type, candidate in by_type.items():
        base = candidate.total * ctx.score_discount
        if hand_type in already_played_types:
            # Already primed — just one play gets x3
            pair_total = base * 3
        else:
            # Need two plays: base + base*3
            pair_total = base + base * 3

        if pair_total > best_pair_score:
            best_pair_score = pair_total
            best_type = hand_type

    # Only recommend Card Sharp setup if it's significantly better than
    # just playing the best hand twice.
    best_score = ctx.best.total * ctx.score_discount if ctx.best else 0
    if best_type and best_pair_score > best_score * 2.5:
        return best_type

    return None


def _selzer_remaining(jokers: list) -> int | None:
    """Return how many hands Selzer has left before self-destructing, or None."""
    for j in jokers:
        if joker_key(j) == "j_selzer":
            extra = j.value.ability.extra
            return int(extra) if extra is not None else 10
    return None


def _loyalty_fires_in(jokers: list) -> int | None:
    """Return how many hands until Loyalty Card fires, or None if not owned.

    loyalty_remaining == 0 means it fires THIS hand (0 hands away).
    loyalty_remaining == 2 means it fires in 2 hands.
    """
    for j in jokers:
        if joker_key(j) == "j_loyalty_card":
            remaining = j.value.ability.loyalty_remaining
            if remaining is not None:
                return int(remaining)
            return None
    return None


def _loyalty_cycle(jokers: list) -> int:
    """Return the Loyalty Card cycle length (default 4 — fires every 4 hands)."""
    for j in jokers:
        if joker_key(j) == "j_loyalty_card":
            extra = j.value.ability.extra
            # The cycle is typically extra + 1 (fires every N+1 hands)
            return int(extra if extra is not None else 3) + 1
    return 4


def _plan_safety_margin(ctx: RoundContext) -> float:
    """Compute safety margin for the plan's projected total."""
    if ctx.blind_name == "The Wall":
        return _PLAN_MARGIN_WALL
    outlook = ctx.round_outlook
    if outlook in ("comfortable", "won"):
        return _PLAN_MARGIN_COMFORTABLE
    return _PLAN_MARGIN_TIGHT
