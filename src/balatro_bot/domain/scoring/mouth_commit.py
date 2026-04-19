"""Pre-lock commitment for The Mouth: pick the best round-total hand type.

The Mouth locks your hand type on the first played hand. Before that lock,
we pick the type that maximizes expected round total (first play + repeated
follow-ups), not the type with the best single-hand score.

Two commitment modes:

  - **Formable**: a target type T is already playable from the current 8
    cards. First-hand score is known precisely; follow-ups are discounted
    by p_repeat(T).

  - **Chase**: T isn't formable now, but discarding non-contributing cards
    and drawing replacements gives some chance of forming it. First-hand
    outcome is Monte Carlo averaged via `best_hand(..., required_hand=T)`.
    On a missed chase under Mouth pre-lock we do NOT play (would lock the
    type) — we re-plan. The MC re-scores the reformed hand without the T
    constraint to estimate that fallback.

The argmax runs across both formable and chase candidates in one pass.
When nothing is formable and discards exist, a chase is forced (any chase
beats no commit at all, since greedy play would lock Mouth to a junk type).
"""

from __future__ import annotations

import logging
import random
from math import comb

from balatro_bot.cards import joker_key
from balatro_bot.domain.scoring.draws import (
    flush_draw_quality, flush_draw_quality_loose,
    four_kind_draw_quality, five_kind_draw_quality,
    flush_five_draw_quality, flush_house_draw_quality,
    full_house_draw_quality,
    pair_draw_quality, straight_draw_quality, straight_flush_draw_quality,
    three_kind_draw_quality, two_pair_draw_quality,
)
from balatro_bot.domain.scoring.search import best_hand, enumerate_hands

log = logging.getLogger("balatro_bot")

# MC samples per chase candidate. Matches discard_policy.N_SAMPLES.
_N_SAMPLES = 30

# If the draw-quality helper's analytical hit prob falls below this, skip
# the MC eval. A chase below ~10% hit is nearly always dominated by any
# formable option, and MC on such low-signal samples is noisy.
_MIN_CHASE_PRE_FILTER = 0.10

_HAND_SIZE = 8  # standard draw; close enough for ranking purposes


# P(reforming each hand type on a fresh 8-card draw). Static baselines used
# when deck composition doesn't meaningfully shift the answer. Flush and
# Straight get deck-aware refinement below.
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


# Types we'll consider for chasing when not formable. Ordered rough-strong-
# to-rough-weak, but the final pick is argmax, so order doesn't matter.
_CHASEABLE_TYPES: tuple[str, ...] = (
    "Flush Five", "Flush House", "Five of a Kind", "Straight Flush",
    "Four of a Kind", "Full House", "Flush", "Straight",
    "Three of a Kind", "Two Pair", "Pair",
)


def choose_mouth_commit(
    hand_cards,
    hand_levels,
    jokers,
    joker_limit: int,
    hands_left: int,
    discards_left: int,
    money: int,
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

    Evaluates every formable type and every chase-reachable type, then
    argmaxes on expected round total. Returns None only when no candidate
    is available at all (empty hand or fully locked out).
    """
    if hands_left <= 0 or not hand_cards:
        return None

    joker_keys_set = {joker_key(j) for j in (jokers or [])}

    # --- Pass 1: formable candidates ---
    required_card_indices = {forced_card_idx} if forced_card_idx is not None else None

    formable = enumerate_hands(
        hand_cards, hand_levels,
        jokers=jokers, joker_limit=joker_limit,
        hands_left=hands_left,
        required_card_indices=required_card_indices,
        ancient_suit=ancient_suit,
        deck_cards=deck_cards,
        blind_name=blind_name,
        ox_most_played=ox_most_played,
        idol_rank=idol_rank,
        idol_suit=idol_suit,
    )
    best_per_type: dict[str, object] = {}
    for c in formable:
        existing = best_per_type.get(c.hand_name)
        if existing is None or c.total > existing.total:
            best_per_type[c.hand_name] = c

    scored: list[tuple[float, str, str]] = []  # (rt, hand_type, label)
    for hand_type, cand in best_per_type.items():
        p_rep = _repeatability(hand_type, deck_profile, joker_keys_set)
        rt = cand.total * (1.0 + max(0, hands_left - 1) * p_rep)
        scored.append((rt, hand_type, f"formable(score={cand.total}, p_rep={p_rep:.2f})"))

    # --- Pass 2: chase candidates ---
    # Skip chases already covered by a formable of the same type (see PR3
    # design note: chasing a higher-scoring version of a formable type is a
    # rabbit hole and skipped for now). When no formable exists at all, drop
    # the hit-prob pre-filter so we force some chase rather than returning None.
    if discards_left > 0 and deck_cards:
        has_formable = bool(best_per_type)
        pre_filter = _MIN_CHASE_PRE_FILTER if has_formable else 0.0

        ctx_params = _ChaseParams(
            hand_cards=hand_cards,
            hand_levels=hand_levels,
            jokers=jokers,
            joker_limit=joker_limit,
            money=money,
            discards_left=discards_left,
            hands_left=hands_left,
            deck_cards=deck_cards,
            ancient_suit=ancient_suit,
            idol_rank=idol_rank,
            idol_suit=idol_suit,
            blind_name=blind_name,
            ox_most_played=ox_most_played,
            required_card_indices=required_card_indices,
        )

        for hand_type in _CHASEABLE_TYPES:
            if hand_type in best_per_type:
                continue  # already evaluated as formable
            keep = _chase_keep_set(hand_type, hand_cards, deck_cards, joker_keys_set)
            if keep is None:
                continue
            keep_indices, analytical_prob = keep
            if analytical_prob < pre_filter:
                continue
            p_rep = _repeatability(hand_type, deck_profile, joker_keys_set)
            rt, p_form = _mc_chase_rt(
                keep_indices, hand_type, p_rep, hands_left, ctx_params,
            )
            scored.append((
                rt, hand_type,
                f"chase(keep={len(keep_indices)}, p_form={p_form:.2f}, p_rep={p_rep:.2f})",
            ))

    if not scored:
        return None

    scored.sort(key=lambda x: -x[0])
    best_rt, best_type, best_label = scored[0]
    log.info(
        "MouthCommit: %s rt=%d | %s | %d candidates evaluated",
        best_type, int(best_rt), best_label, len(scored),
    )
    return best_type


# ---------------------------------------------------------------------------
# Chase evaluation
# ---------------------------------------------------------------------------


class _ChaseParams:
    """Bundle of parameters threaded into MC best_hand calls."""
    __slots__ = (
        "hand_cards", "hand_levels", "jokers", "joker_limit", "money",
        "discards_left", "hands_left", "deck_cards", "ancient_suit",
        "idol_rank", "idol_suit", "blind_name", "ox_most_played",
        "required_card_indices",
    )

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


def _chase_keep_set(
    hand_type: str,
    hand_cards,
    deck_cards,
    joker_keys_set: set[str],
):
    """Dispatch to the right draws.py helper for a chase target type.

    Returns (keep_indices, analytical_prob) or None if no path exists.
    """
    four_fingers = "j_four_fingers" in joker_keys_set
    shortcut = "j_shortcut" in joker_keys_set
    smeared = "j_smeared" in joker_keys_set

    result = None
    if hand_type == "Pair":
        result = pair_draw_quality(hand_cards, deck_cards)
    elif hand_type == "Two Pair":
        result = two_pair_draw_quality(hand_cards, deck_cards)
    elif hand_type == "Three of a Kind":
        result = three_kind_draw_quality(hand_cards, deck_cards)
    elif hand_type == "Straight":
        result = straight_draw_quality(hand_cards, deck_cards, shortcut=shortcut)
    elif hand_type == "Flush":
        result = (
            flush_draw_quality(hand_cards, deck_cards, smeared=smeared)
            or flush_draw_quality_loose(hand_cards, deck_cards, smeared=smeared)
        )
    elif hand_type == "Full House":
        result = full_house_draw_quality(hand_cards, deck_cards)
    elif hand_type == "Four of a Kind":
        result = four_kind_draw_quality(hand_cards, deck_cards)
    elif hand_type == "Straight Flush":
        result = straight_flush_draw_quality(
            hand_cards, deck_cards, shortcut=shortcut, smeared=smeared,
        )
    elif hand_type == "Five of a Kind":
        result = five_kind_draw_quality(hand_cards, deck_cards)
    elif hand_type == "Flush House":
        result = flush_house_draw_quality(hand_cards, deck_cards, smeared=smeared)
    elif hand_type == "Flush Five":
        result = flush_five_draw_quality(hand_cards, deck_cards, smeared=smeared)

    if result is None:
        return None
    # Helpers return (keep, prob) or (keep, prob, suit). First two are stable.
    keep, prob = result[0], result[1]
    if not keep or prob <= 0:
        return None
    return keep, prob


def _mc_chase_rt(
    keep_indices: list[int],
    target_type: str,
    p_repeat: float,
    hands_left: int,
    ctx: _ChaseParams,
) -> tuple[float, float]:
    """Monte Carlo estimate of round-total EV for a chase commit.

    For each sample: draw replacements for the non-kept cards and score
    `best_hand(required_hand=target_type)`. Hits contribute to score_on_hit.
    Misses re-score the reformed hand unconstrained to estimate the fallback
    round-total we'd achieve after re-planning.

    Returns (rt_chase, p_form).
    """
    keep_cards = [ctx.hand_cards[i] for i in keep_indices]
    discard_count = len(ctx.hand_cards) - len(keep_cards)
    draw_pile = ctx.deck_cards

    if not draw_pile or len(draw_pile) < discard_count or discard_count < 0:
        return 0.0, 0.0

    hits = 0
    score_on_hit_sum = 0.0
    rt_on_miss_sum = 0.0

    for _ in range(_N_SAMPLES):
        drawn = random.sample(draw_pile, discard_count)
        new_hand = keep_cards + drawn

        hit_result = best_hand(
            new_hand,
            hand_levels=ctx.hand_levels,
            jokers=ctx.jokers, joker_limit=ctx.joker_limit,
            money=ctx.money,
            discards_left=max(0, ctx.discards_left - 1),
            hands_left=ctx.hands_left,
            required_hand=target_type,
            ancient_suit=ctx.ancient_suit,
            idol_rank=ctx.idol_rank,
            idol_suit=ctx.idol_suit,
            deck_cards=ctx.deck_cards,
            blind_name=ctx.blind_name,
            ox_most_played=ctx.ox_most_played,
        )

        if hit_result is not None and hit_result.total > 0:
            hits += 1
            score_on_hit_sum += hit_result.total
        else:
            # Miss: re-plan to best formable from the reformed hand.
            miss_result = best_hand(
                new_hand,
                hand_levels=ctx.hand_levels,
                jokers=ctx.jokers, joker_limit=ctx.joker_limit,
                money=ctx.money,
                discards_left=max(0, ctx.discards_left - 1),
                hands_left=ctx.hands_left,
                ancient_suit=ctx.ancient_suit,
                idol_rank=ctx.idol_rank,
                idol_suit=ctx.idol_suit,
                deck_cards=ctx.deck_cards,
                blind_name=ctx.blind_name,
                ox_most_played=ctx.ox_most_played,
            )
            if miss_result is not None:
                # Approximate the fallback's own repeatability via our static
                # model — cheap enough vs. rebuilding deck_profile per sample.
                fallback_type = miss_result.hand_name
                fallback_p_rep = _REPEAT_STATIC.get(fallback_type, 0.1)
                rt_on_miss_sum += miss_result.total * (
                    1.0 + max(0, hands_left - 1) * fallback_p_rep
                )

    p_form = hits / _N_SAMPLES
    misses = _N_SAMPLES - hits
    score_on_hit = score_on_hit_sum / hits if hits else 0.0
    rt_on_miss = rt_on_miss_sum / misses if misses else 0.0

    rt_success = score_on_hit * (1.0 + max(0, hands_left - 1) * p_repeat)
    rt_chase = p_form * rt_success + (1.0 - p_form) * rt_on_miss
    return rt_chase, p_form


# ---------------------------------------------------------------------------
# Repeatability model
# ---------------------------------------------------------------------------


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
            base *= 3
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

    p_below = 0.0
    for k in range(need):
        if k <= good and (draws - k) <= bad:
            p_below += comb(good, k) * comb(bad, draws - k) / total_combos

    return max(0.0, min(1.0, 1.0 - p_below))
