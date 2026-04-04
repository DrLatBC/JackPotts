"""Consumable-phase policy functions — pure decision logic extracted from rules.

Contains scoring and evaluation logic for consumable use decisions.
Rules in rules/consumables.py become thin wrappers around these.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from balatro_bot.cards import card_rank, card_suit, card_suits, _modifier, rank_value
from balatro_bot.constants import (
    PLANET_KEYS, SAFE_CONSUMABLE_TAROTS, TARGETING_TAROTS,
    IMMEDIATE_TARGETING, SAFE_SPECTRAL_CONSUMABLES, SPECTRAL_TARGETING,
    SCALING_JOKERS, FACE_RANKS_TAROT,
)
from balatro_bot.domain.scoring.draws import flush_draw
from balatro_bot.domain.scoring.estimate import score_hand
from balatro_bot.domain.scoring.search import best_hand

if TYPE_CHECKING:
    from typing import Any
    from balatro_bot.strategy import Strategy
    from balatro_bot.domain.scoring.search import HandCandidate

log = logging.getLogger("balatro_bot")


# ---------------------------------------------------------------------------
# Dynamic consumable value scoring (moved from _helpers.py)
# ---------------------------------------------------------------------------


def score_consumable(
    key: str,
    state: dict[str, Any],
    strat: Strategy | None = None,
) -> float:
    """Score a consumable by key, accounting for game state.

    Used by buy, use, and pack pick logic for consistent valuation.
    Returns a float where higher = more valuable. 0 or negative = skip.
    """
    jokers = state.get("jokers", {}).get("cards", [])
    joker_slots = state.get("jokers", {})
    money = state.get("money", 0)
    ante = state.get("ante_num", 1)

    # --- Planet cards ---
    if key in PLANET_KEYS:
        hand_type = PLANET_KEYS[key]
        if hand_type == "ALL":
            return 8.0  # Black Hole — always top priority
        hand_levels = state.get("hands", {})
        has_constellation = any(j.get("key") == "j_constellation" for j in jokers)
        affinity = strat.hand_affinity(hand_type) if strat else 0.0
        if affinity > 0:
            score = 5.0 + affinity
            if has_constellation:
                score += 2.0
            return score
        if has_constellation:
            return 3.0  # every planet = +0.1 xmult
        return 0.0  # off-strategy, no constellation

    # --- No-target tarots ---
    if key == "c_judgement":
        slots_open = joker_slots.get("count", 0) < joker_slots.get("limit", 5)
        return 6.0 if slots_open else 0.0
    if key == "c_high_priestess":
        return 5.0
    if key == "c_hermit":
        return min(money, 20) / 4.0
    if key == "c_emperor":
        cons = state.get("consumables", {})
        slots_open = cons.get("count", 0) < cons.get("limit", 2)
        return 3.0 if slots_open else 1.0
    if key == "c_temperance":
        total_sell = sum(
            j.get("cost", {}).get("sell", 0) if isinstance(j.get("cost"), dict) else 0
            for j in jokers
        )
        return min(total_sell, 20) / 4.0
    if key == "c_wheel_of_fortune":
        eligible = [j for j in jokers if not (isinstance(j.get("modifier"), dict) and j["modifier"].get("edition"))]
        n = len(eligible)
        return 3.0 if n >= 3 else (1.5 if n >= 1 else 0.0)
    if key == "c_fool":
        # Fool copies the last tarot/planet used. For buy/pack decisions we
        # don't know what it'll copy — give it a modest base value since it
        # has potential. Actual use decisions are handled in UseConsumables
        # which tracks _last_used_consumable and scores Fool dynamically.
        return 1.5

    # --- Targeting tarots ---
    if key in TARGETING_TAROTS:
        max_count, effect_type, extra = TARGETING_TAROTS[key]
        return _score_targeting_tarot(key, effect_type, extra, state, strat)

    # Unknown consumable
    return 0.0


def _score_targeting_tarot(
    key: str,
    effect_type: str,
    extra: str | None,
    state: dict[str, Any],
    strat: Strategy | None,
) -> float:
    """Score a targeting tarot based on effect type and game state."""
    ante = state.get("ante_num", 1)
    # Estimate remaining rounds: (8 - ante) * 3 rounds per ante (rough)
    remaining_rounds = max(1, (8 - ante) * 3)

    if effect_type == "clone":
        # Death: score gap between best and worst card — higher gap = more value
        return 4.0

    if effect_type == "destroy":
        # Hanged Man: deck thinning — more valuable early (more draws to benefit)
        return 3.0 if ante <= 4 else 2.0

    if effect_type == "glass":
        # Justice: x2 mult on scoring card — always strong
        return 4.5

    if effect_type == "suit_convert":
        # Suit conversions: value depends on suit affinity
        suit_aff = strat.suit_affinity(extra) if strat and extra else 0.0
        if suit_aff <= 0:
            return 0.5  # no suit strategy — low value
        return 2.0 + suit_aff

    if effect_type == "enhance":
        # Enhancements by type
        enhance_scores = {
            "Lucky": 3.5, "Steel": 3.0, "Mult": 2.5,
            "Bonus": 2.0, "Wild": 2.5, "Gold": 0.0,  # Gold scored separately
        }
        base = enhance_scores.get(extra, 2.0)
        # Synergy bonuses for enhancement-caring jokers
        jokers = state.get("jokers", {}).get("cards", [])
        joker_keys = {j.get("key") for j in jokers}
        if extra == "Lucky":
            if "j_lucky_cat" in joker_keys:
                base += 2.0
            if "j_oops" in joker_keys:
                base += 1.5
        elif extra == "Steel":
            if "j_steel_joker" in joker_keys:
                base += 2.0
        # Enhancements compound — more valuable early
        if ante <= 3:
            base *= 1.3
        return base

    if effect_type == "gold":
        # Devil: $3 per round remaining on a held card
        return remaining_rounds * 3 / 4.0

    if effect_type == "stone":
        # Tower: niche — only good with Stone Joker
        has_stone_joker = any(j.get("key") == "j_stone"
                             for j in state.get("jokers", {}).get("cards", []))
        return 3.0 if has_stone_joker else 1.0

    if effect_type == "rank_up":
        # Strength: situational, can damage rank affinity
        return 1.0

    return 1.5  # fallback for other effect types


# ---------------------------------------------------------------------------
# Hex evaluation (moved from _helpers.py)
# ---------------------------------------------------------------------------


def evaluate_hex(jokers: list[dict], ante: int, hand_levels: dict) -> float:
    """Score Hex usage/pick. Returns 0.0 to skip, or positive score.

    Hex adds Polychrome (x1.5) to a random joker and destroys the rest.
    Worth it when one joker dominates the roster and the rest are expendable.
    """
    from balatro_bot.strategy import compute_strategy
    from balatro_bot.domain.policy.shop_valuation import evaluate_joker_value

    if not jokers:
        return 0.0
    if len(jokers) <= 1:
        return 3.5  # 1 joker: free Polychrome, great

    owned_keys = {j.get("key") for j in jokers}
    # Never use with multiple scaling jokers — losing any is unacceptable
    scaling_count = len(owned_keys & SCALING_JOKERS)
    if scaling_count >= 2:
        return 0.0

    # Evaluate each joker's value
    strat = compute_strategy(jokers, hand_levels)
    values = []
    for j in jokers:
        val = evaluate_joker_value(j, jokers, hand_levels, ante, strat)
        values.append((j, val))
    values.sort(key=lambda x: x[1], reverse=True)

    best_val = values[0][1]
    rest_total = sum(v for _, v in values[1:])

    # Hex is worth it when:
    # - Best joker is strong enough to carry (>= 5.0 value)
    # - AND the rest aren't worth more than the Polychrome upgrade
    # Polychrome adds x1.5 permanently — roughly worth 4 value points
    polychrome_value = 4.0
    if best_val < 5.0:
        return 0.0  # best joker not strong enough to solo
    if rest_total > polychrome_value + 2.0:
        return 0.0  # sacrificing too much

    # Scale score by how dominant the best joker is
    dominance = best_val / max(rest_total, 1.0)
    score = 3.5 + dominance

    # Late-game soft penalty: harder to rebuild, need a truly dominant joker
    if ante >= 7:
        score *= 0.4
    elif ante >= 6:
        score *= 0.5
    elif ante >= 5:
        score *= 0.75

    return min(score, 8.0)


# ---------------------------------------------------------------------------
# Use-now scoring (extracted from UseConsumables._score_use_now)
# ---------------------------------------------------------------------------


def score_use_now(
    idx: int, key: str, label: str, card: dict,
    state: dict, strat: Strategy, hand_cards: list, jokers: list,
    hand_levels: dict, current_best: HandCandidate | None, current_score: int,
    chips_remaining: int, can_win_now: bool, desperate: bool,
    money: int, discards_left: int, hands_left: int,
    joker_limit: int, ante: int,
    last_used_consumable: str | None = None,
) -> tuple[float, tuple | None]:
    """Return (use_value, action_args) for using this consumable right now.

    Returns action_args as a tuple describing the action, or None if not usable.
    The caller (rule) is responsible for constructing the actual Action object.

    action_args format:
      ("use", idx, reason)                     — UseConsumable(idx, reason=...)
      ("use_target", idx, targets, reason)     — UseConsumable(idx, target_cards=targets, reason=...)
      ("rearrange", order, reason)             — RearrangeHand(order=order, reason=...)
    """
    from balatro_bot.rules._helpers import (
        _find_tarot_targets, _find_gold_targets, _find_enhancement_targets,
        _find_clone_targets,
    )

    joker_slots = state.get("jokers", {})

    # --- Planet cards: always use immediately ---
    if key in PLANET_KEYS:
        hand_type = PLANET_KEYS[key]
        affinity = strat.hand_affinity(hand_type) if hand_type != "ALL" else 99
        cur_level = hand_levels.get(hand_type, {}).get("level", 1) if hand_type != "ALL" else 0
        return (10.0, ("use", idx,
            f"[PLANET] {label}: {hand_type} lv{cur_level}→lv{cur_level+1} (affinity={affinity:.0f})"))

    # --- Safe no-target tarots: use immediately ---
    if key in SAFE_CONSUMABLE_TAROTS:
        if key == "c_judgement":
            if joker_slots.get("count", 0) >= joker_slots.get("limit", 5):
                return (0.0, None)
        if key == "c_wheel_of_fortune":
            if not jokers:
                return (0.0, None)
            # NOT_ALLOWED if every joker already has an edition
            if all(isinstance(j.get("modifier"), dict) and j["modifier"].get("edition") for j in jokers):
                return (0.0, None)
        if key == "c_fool":
            if not last_used_consumable:
                return (0.0, None)  # nothing used yet — API will reject
            # Score Fool as whatever it copies
            value = score_consumable(last_used_consumable, state, strat)
            return (value, ("use", idx,
                f"use Fool (copies {last_used_consumable}, value={value:.1f})"))
        value = score_consumable(key, state, strat)
        return (value, ("use", idx, f"use tarot: {label} (value={value:.1f})"))

    # --- Safe spectral cards ---
    if key in SAFE_SPECTRAL_CONSUMABLES:
        if key in ("c_ankh", "c_hex") and not jokers:
            return (0.0, None)
        if key == "c_hex":
            hex_val = evaluate_hex(jokers, ante, hand_levels)
            if hex_val <= 0.0:
                return (0.0, None)
            return (hex_val, ("use", idx, f"use Hex (score={hex_val:.1f})"))
        if key == "c_ankh" and joker_slots.get("count", 0) >= joker_slots.get("limit", 5):
            return (0.0, None)
        if key == "c_wraith" and joker_slots.get("count", 0) >= joker_slots.get("limit", 5):
            return (0.0, None)
        if key == "c_ectoplasm" and ante < 3:
            return (0.0, None)
        return (5.0, ("use", idx, f"use spectral: {label}"))

    # --- Targeting tarots/spectrals ---
    if not hand_cards:
        return (0.0, None)

    # Check Tarots
    if key in TARGETING_TAROTS:
        max_count, effect_type, extra = TARGETING_TAROTS[key]
        return _score_targeting_use(
            idx, key, label, max_count, effect_type, extra,
            state, strat, hand_cards, jokers, hand_levels,
            current_best, current_score, chips_remaining,
            can_win_now, desperate, money, discards_left, hands_left,
            joker_limit, ante,
        )

    # Check Spectral targeting
    if key in SPECTRAL_TARGETING:
        max_count, effect_type, extra = SPECTRAL_TARGETING[key]
        targets, score = _find_tarot_targets(
            effect_type, extra, max_count, hand_cards, jokers, strat,
            current_best=current_best,
        )
        if targets:
            return (score, ("use_target", idx, targets,
                f"use spectral: {label} -> targets {targets}"))

    return (0.0, None)


def _score_targeting_use(
    idx: int, key: str, label: str,
    max_count: int, effect_type: str, extra: str | None,
    state: dict, strat: Strategy, hand_cards: list, jokers: list,
    hand_levels: dict, current_best: HandCandidate | None, current_score: int,
    chips_remaining: int, can_win_now: bool, desperate: bool,
    money: int, discards_left: int, hands_left: int,
    joker_limit: int, ante: int,
) -> tuple[float, tuple | None]:
    """Score and return action args for a targeting tarot."""
    from balatro_bot.rules._helpers import (
        _find_tarot_targets, _find_gold_targets, _find_enhancement_targets,
        _find_clone_targets,
    )

    # --- Immediate-use targeting (destroy, clone, stone, rank_up) ---
    if effect_type in IMMEDIATE_TARGETING:
        # Death tarot: rearrange hand if needed
        if effect_type == "clone":
            result = _find_clone_targets(hand_cards, strat)
            if result is None:
                return (0.0, None)
            best_idx, worst_idx = result
            if best_idx > worst_idx:
                order = list(range(len(hand_cards)))
                order.remove(best_idx)
                insert_pos = order.index(worst_idx)
                order.insert(insert_pos, best_idx)
                return (4.0, ("rearrange", order,
                    f"rearrange for Death: move {hand_cards[best_idx].get('label','?')} left of {hand_cards[worst_idx].get('label','?')}"))
            return (4.0, ("use_target", idx, [best_idx, worst_idx],
                f"Death: clone {hand_cards[best_idx].get('label','?')} onto {hand_cards[worst_idx].get('label','?')}"))

        targets, score = _find_tarot_targets(
            effect_type, extra, max_count, hand_cards, jokers, strat,
            current_best=current_best,
        )
        if targets:
            return (score, ("use_target", idx, targets,
                f"use tarot: {label} -> targets {targets}"))
        return (0.0, None)

    # --- Tactical targeting (suit_convert, glass, enhance, gold) ---

    # Gold enhancement: fire when round already won
    if effect_type == "gold":
        if can_win_now:
            targets = _find_gold_targets(hand_cards, max_count, current_best)
            if targets:
                return (4.0, ("use_target", idx, targets,
                    f"gold junk: {label} on held card (round already won)"))
        return (0.0, None)

    # Suit conversions: fire when it creates a meaningful improvement
    if effect_type == "suit_convert":
        result = eval_suit_convert(
            hand_cards, hand_levels, jokers, extra, max_count,
            current_score, strat, money, discards_left, hands_left,
            joker_limit=joker_limit,
        )
        if result:
            new_score, targets = result
            improvement = new_score / max(current_score, 1)
            if improvement > 1.2 or desperate:
                return (improvement * 3.0, ("use_target", idx, targets,
                    f"tactical: {label} -> Flush ({new_score} vs {current_score}, {hands_left}h left)"))
        return (0.0, None)

    # Enhancements and Glass: use based on ante timing
    if effect_type in ("glass", "enhance"):
        # Ante-based timing
        if ante <= 3:
            threshold = 1.0
        elif ante <= 5:
            threshold = 1.2
        else:
            if not desperate:
                return (0.0, None)
            threshold = 1.0

        if effect_type == "glass":
            result = eval_glass(hand_cards, hand_levels, jokers, current_best, current_score, strat=strat)
            if result:
                new_score, targets = result
                improvement = new_score / max(current_score, 1)
                if improvement >= threshold or desperate:
                    return (improvement * 3.0, ("use_target", idx, targets,
                        f"{'desperate' if desperate else 'tactical'}: Glass (×{improvement:.1f}, {hands_left}h left)"))
        else:
            result = eval_enhancement(
                hand_cards, hand_levels, jokers, current_best, current_score,
                extra, max_count, strat=strat,
            )
            if result:
                new_score, targets = result
                improvement = new_score / max(current_score, 1)
                if improvement >= threshold or desperate:
                    return (improvement * 3.0, ("use_target", idx, targets,
                        f"{'desperate' if desperate else 'tactical'}: {label} ({extra}) (×{improvement:.1f}, {hands_left}h left)"))

    return (0.0, None)


# ---------------------------------------------------------------------------
# Hold-value scoring (extracted from UseConsumables._score_hold)
# ---------------------------------------------------------------------------


def score_hold(
    key: str, ante: int, slots_full: bool,
    desperate: bool, hands_left: int, discards_left: int,
) -> float:
    """Score the value of holding this consumable for later."""
    # Planets: always use immediately
    if key in PLANET_KEYS:
        return 0.0
    # No-target tarots: always use immediately
    if key in SAFE_CONSUMABLE_TAROTS:
        return 0.0
    # Spectrals: always use immediately
    if key in SAFE_SPECTRAL_CONSUMABLES:
        return 0.0

    # Tactical consumables: hold value depends on ante timing
    hold = 0.0

    if key in TARGETING_TAROTS:
        _, effect_type, extra = TARGETING_TAROTS[key]

        if effect_type == "suit_convert":
            # Worth holding if we have discards (might draw into flush)
            hold = 2.0 if discards_left > 0 else 0.5

        elif effect_type in ("glass", "enhance"):
            # Early: low hold value (use freely, compound)
            # Late: high hold value (wait for best target)
            if ante <= 3:
                hold = 1.0
            elif ante <= 5:
                hold = 2.0
            else:
                hold = 3.0

        elif effect_type == "gold":
            # Hold until winning
            hold = 2.0

        else:
            # Immediate-use tarots (destroy, clone, etc)
            hold = 0.0

    # Slot pressure: holding a mediocre card blocks better purchases
    if slots_full:
        hold -= 2.0

    return hold


# ---------------------------------------------------------------------------
# Evaluation helpers (extracted from UseConsumables methods)
# ---------------------------------------------------------------------------


def eval_suit_convert(
    hand_cards, hand_levels, jokers, target_suit, max_count,
    current_score, strat, money=0, discards_left=0, hands_left=1,
    joker_limit: int = 5,
) -> tuple[int, list[int]] | None:
    """Would converting cards to target_suit create a Flush?"""
    matching = []
    non_matching = []
    for i, c in enumerate(hand_cards):
        suits = card_suits(c)
        if target_suit in suits:
            matching.append(i)
        elif card_suit(c) is not None:
            non_matching.append(i)

    needed = 5 - len(matching)
    if needed <= 0:
        return None
    if needed > min(max_count, len(non_matching)):
        return None

    rank_aff = strat.rank_affinity_dict() if strat else {}
    non_matching.sort(key=lambda i: (
        rank_aff.get(card_rank(hand_cards[i]) or "", 0.0),
        rank_value(card_rank(hand_cards[i]) or "2"),
    ))
    targets = non_matching[:needed]

    flush_cards = matching[:5 - needed]
    flush_cards.extend(targets)
    flush_cards = flush_cards[:5]
    simulated = [hand_cards[i] for i in flush_cards]
    held = [hand_cards[i] for i in range(len(hand_cards)) if i not in set(flush_cards)]
    _, _, flush_score = score_hand(
        "Flush", simulated, hand_levels,
        jokers=jokers, played_cards=simulated, held_cards=held,
        money=money, discards_left=discards_left, hands_left=hands_left,
        joker_limit=joker_limit,
    )

    return (flush_score, targets)


def eval_glass(
    hand_cards, hand_levels, jokers, current_best, current_score,
    strat: Strategy | None = None,
) -> tuple[int, list[int]] | None:
    """Would applying Glass to a card in our best hand boost scoring?"""
    if not current_best:
        return None

    rank_aff = strat.rank_affinity_dict() if strat else {}
    # Score each unenhanced card in the scoring hand
    candidates = []
    for idx in current_best.card_indices:
        c = hand_cards[idx]
        r = card_rank(c)
        if not r or _modifier(c).get("enhancement"):
            continue
        aff = rank_aff.get(r, 0.0)
        is_face = r in FACE_RANKS_TAROT
        # Sort key: high affinity first, then face cards, then high chip value
        candidates.append((idx, -aff, 0 if is_face else 1, -rank_value(r)))

    if not candidates:
        return None

    candidates.sort(key=lambda x: (x[1], x[2], x[3]))
    best_idx = candidates[0][0]
    is_face = candidates[0][2] == 0
    multiplier = 1.8 if is_face else 1.6
    estimated_new = int(current_score * multiplier)
    return (estimated_new, [best_idx])


def eval_enhancement(
    hand_cards, hand_levels, jokers, current_best, current_score,
    enhancement, max_count, strat: Strategy | None = None,
) -> tuple[int, list[int]] | None:
    """Would applying this enhancement boost cards about to score?"""
    from balatro_bot.rules._helpers import _find_enhancement_targets

    if not current_best:
        return None

    rank_aff = strat.rank_affinity_dict() if strat else None

    # Wild (Lovers): check if it enables a Flush
    if enhancement == "Wild":
        fd = flush_draw(hand_cards)
        if fd and len(fd) >= 4:
            for i, c in enumerate(hand_cards):
                if i not in fd and not _modifier(c).get("enhancement"):
                    estimated = int(current_score * 1.5)
                    return (estimated, [i])

    # Steel: apply to highest-rank held (not played) card
    if enhancement == "Steel":
        scoring_set = set(current_best.card_indices)
        held = [i for i, c in enumerate(hand_cards)
                if i not in scoring_set and not _modifier(c).get("enhancement")]
        if held:
            RANK_ORDER = ["2", "3", "4", "5", "6", "7", "8", "9", "T", "J", "Q", "K", "A"]
            held.sort(key=lambda i: RANK_ORDER.index(card_rank(hand_cards[i]))
                      if card_rank(hand_cards[i]) in RANK_ORDER else -1, reverse=True)
            estimated = int(current_score * 1.5)
            return (estimated, held[:max_count])
        return None

    # Other enhancements: apply to highest-value scoring card, prefer affinity ranks
    targets = _find_enhancement_targets(hand_cards, max_count, rank_affinity=rank_aff)
    if targets:
        scoring_set = set(current_best.card_indices)
        relevant = [t for t in targets if t in scoring_set]
        if relevant:
            estimated = int(current_score * 1.2)
            return (estimated, relevant[:max_count])

    return None
