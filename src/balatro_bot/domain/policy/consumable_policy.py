"""Consumable-phase policy functions — pure decision logic extracted from rules.

Contains scoring and evaluation logic for consumable use decisions.
Rules in rules/consumables.py become thin wrappers around these.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from balatro_bot.cards import card_rank, card_suit, card_suits, _modifier, joker_key, rank_value
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
# Consumable value scoring constants
# ---------------------------------------------------------------------------

# Planet cards
_BLACK_HOLE_VALUE = 8.0            # Black Hole levels every hand — top priority
_PLANET_BASE_VALUE = 5.0           # base score for on-strategy planet
_CONSTELLATION_BONUS = 2.0         # extra value when Constellation owned
_CONSTELLATION_ONLY_VALUE = 3.0    # off-strategy planet with Constellation (+0.1 xmult)

# Generic planet floor: planets for common hands that the bot realistically
# plays (Pair, Two Pair, Three of a Kind, High Card) are valuable even without
# joker-driven affinity. Leveling them compounds for the whole run.
_COMMON_HAND_FLOOR = 3.0
_COMMON_HANDS = frozenset({"Pair", "Two Pair", "Three of a Kind", "High Card"})
# Weaker floor for hands that do show up but less often.
_UNCOMMON_HAND_FLOOR = 2.0
_UNCOMMON_HANDS = frozenset({"Straight", "Flush", "Full House", "Four of a Kind"})

# No-target tarot values
_JUDGEMENT_VALUE = 6.0             # creates a random joker
_HIGH_PRIESTESS_VALUE = 5.0        # creates up to 2 random planet cards
_HERMIT_MONEY_CAP = 20             # Hermit caps at doubling $20
_HERMIT_DIVISOR = 4.0              # converts capped money to value
_EMPEROR_OPEN_VALUE = 3.0          # Emperor with open consumable slot
_EMPEROR_FULL_VALUE = 1.0          # Emperor when slots are full
_TEMPERANCE_CAP = 20               # Temperance caps at $20 sell value
_TEMPERANCE_DIVISOR = 4.0          # converts capped sell value to score
_WHEEL_HIGH_VALUE = 3.0            # Wheel of Fortune with 3+ eligible jokers
_WHEEL_LOW_VALUE = 1.5             # Wheel of Fortune with 1-2 eligible jokers
_WHEEL_MIN_ELIGIBLE = 3            # threshold for high vs low Wheel value
_FOOL_VALUE = 1.5                  # Fool base buy/pick value (copies last used)

# Targeting tarot values
_DEATH_VALUE = 4.0                 # Death: clone best card onto worst
_HANGED_MAN_EARLY = 3.0            # Hanged Man deck thinning (ante <= cutoff)
_HANGED_MAN_LATE = 2.0             # Hanged Man late game
_HANGED_MAN_ANTE_CUTOFF = 4        # ante boundary for early/late
_JUSTICE_VALUE = 4.5               # Justice: x2 mult on scoring card
_SUIT_CONVERT_NO_AFFINITY = 0.5    # suit conversion with no suit strategy
_STONE_WITH_JOKER = 3.0            # Tower with Stone Joker
_STONE_WITHOUT_JOKER = 1.0         # Tower without Stone Joker
_RANK_UP_VALUE = 1.0               # Strength: situational rank-up
_TARGETING_FALLBACK = 1.5          # fallback for other targeting types

# Enhancement scoring
_ENHANCE_SCORES: dict[str, float] = {
    "Lucky": 3.5, "Steel": 3.0, "Mult": 2.5,
    "Bonus": 2.0, "Wild": 2.5, "Gold": 0.0,
}
_LUCKY_CAT_BONUS = 2.0             # Lucky synergy with Lucky Cat
_OOPS_BONUS = 1.5                  # Lucky synergy with Oops! All 6s
_STEEL_JOKER_BONUS = 2.0           # Steel synergy with Steel Joker
_GLASS_JOKER_BONUS = 2.0           # Glass synergy with Glass Joker (more shatters = more xmult)
_GOLD_JOKER_BONUS = 2.0            # Gold synergy with Golden Joker ($4/gold held per round)
_EARLY_ANTE_ENHANCE_BONUS = 1.3    # enhancement compound bonus early game
_EARLY_ANTE_CUTOFF = 3             # ante threshold for early-game bonus

# Devil (Gold enhancement)
_DEVIL_INCOME_PER_ROUND = 3        # $3 per round remaining
_DEVIL_DIVISOR = 4.0               # converts income to value

# Remaining rounds estimate
_TOTAL_ANTES = 8
_ROUNDS_PER_ANTE = 3

# Hex evaluation
_HEX_SOLO_VALUE = 3.5              # Hex with only 1 joker (free Polychrome)
_HEX_POLYCHROME_VALUE = 4.0        # estimated value of Polychrome upgrade
_HEX_MIN_BEST_VAL = 5.0            # best joker must be at least this to solo
_HEX_MARGIN = 2.0                  # rest must not exceed polychrome + this
_HEX_BASE_SCORE = 3.5              # base score before dominance scaling
_HEX_MAX_SCORE = 8.0               # cap on Hex score
_HEX_ANTE_PENALTIES: dict[int, float] = {  # late-game score multipliers
    7: 0.4, 6: 0.5, 5: 0.75,
}

# Use-now scoring
_PLANET_USE_VALUE = 10.0            # planets are always used immediately
_SAFE_SPECTRAL_USE_VALUE = 5.0      # base use-now value for safe spectrals

# Tactical use thresholds
_SUIT_CONVERT_IMPROVEMENT = 1.2     # 20% improvement threshold for suit convert
_DESPERATE_MIN_IMPROVEMENT = 1.15   # floor even in desperate mode — avoid burning a tarot for <15% gain
_TACTICAL_USE_MULTIPLIER = 3.0      # improvement × this = use value
_GLASS_ANTE_THRESHOLDS: dict[str, float] = {  # enhancement timing thresholds
    "early": 1.0,   # ante <= 3: use freely
    "mid": 1.2,     # ante 4-5: need 20% improvement
    "late": 1.0,    # ante 6+: only when desperate
}

# Hold-value scoring
_HOLD_SUIT_CONVERT_WITH_DISCARDS = 2.0
_HOLD_SUIT_CONVERT_NO_DISCARDS = 0.5
_HOLD_ENHANCE_EARLY = 1.0          # ante <= 3
_HOLD_ENHANCE_MID = 2.0            # ante 4-5
_HOLD_ENHANCE_LATE = 3.0           # ante 6+
_HOLD_GOLD = 2.0                   # hold gold until winning
_HOLD_SLOT_PRESSURE = 2.0          # penalty when consumable slots full

# Glass/enhancement estimation multipliers
_GLASS_MULTIPLIER_FACE = 1.8       # Glass on face card: estimated score boost
_GLASS_MULTIPLIER_OTHER = 1.6      # Glass on non-face card
_WILD_STEEL_MULTIPLIER = 1.5       # Wild/Steel enhancement score estimate
_GENERIC_ENHANCE_MULTIPLIER = 1.2   # other enhancements score estimate


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
            return _BLACK_HOLE_VALUE  # Black Hole — always top priority
        has_constellation = any(joker_key(j) == "j_constellation" for j in jokers)
        affinity = strat.hand_affinity(hand_type) if strat else 0.0
        if affinity > 0:
            score = _PLANET_BASE_VALUE + affinity
            if has_constellation:
                score += _CONSTELLATION_BONUS
            return score
        # No joker-driven affinity: planets for hands we'll actually play still
        # compound for the whole run. Pair/Two Pair/3oK/High Card land every
        # round; leveling them is consistently good even on thin rosters.
        if hand_type in _COMMON_HANDS:
            score = _COMMON_HAND_FLOOR
            if has_constellation:
                score += _CONSTELLATION_BONUS
            return score
        if hand_type in _UNCOMMON_HANDS:
            score = _UNCOMMON_HAND_FLOOR
            if has_constellation:
                score += _CONSTELLATION_BONUS
            return score
        if has_constellation:
            return _CONSTELLATION_ONLY_VALUE
        return 0.0  # exotic hand, no constellation

    # --- No-target tarots ---
    if key == "c_judgement":
        slots_open = joker_slots.get("count", 0) < joker_slots.get("limit", 5)
        return _JUDGEMENT_VALUE if slots_open else 0.0
    if key == "c_high_priestess":
        return _HIGH_PRIESTESS_VALUE
    if key == "c_hermit":
        return min(money, _HERMIT_MONEY_CAP) / _HERMIT_DIVISOR
    if key == "c_emperor":
        cons = state.get("consumables", {})
        slots_open = cons.get("count", 0) < cons.get("limit", 2)
        return _EMPEROR_OPEN_VALUE if slots_open else _EMPEROR_FULL_VALUE
    if key == "c_temperance":
        total_sell = sum(
            j.get("cost", {}).get("sell", 0) if isinstance(j.get("cost"), dict) else 0
            for j in jokers
        )
        return min(total_sell, _TEMPERANCE_CAP) / _TEMPERANCE_DIVISOR
    if key == "c_wheel_of_fortune":
        eligible = [j for j in jokers if not (isinstance(j.get("modifier"), dict) and j["modifier"].get("edition"))]
        n = len(eligible)
        return _WHEEL_HIGH_VALUE if n >= _WHEEL_MIN_ELIGIBLE else (_WHEEL_LOW_VALUE if n >= 1 else 0.0)
    if key == "c_fool":
        # Fool copies the last tarot/planet used. For buy/pack decisions we
        # don't know what it'll copy — give it a modest base value since it
        # has potential. Actual use decisions are handled in UseConsumables
        # which tracks _last_used_consumable and scores Fool dynamically.
        return _FOOL_VALUE

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
    # Estimate remaining rounds
    remaining_rounds = max(1, (_TOTAL_ANTES - ante) * _ROUNDS_PER_ANTE)

    if effect_type == "clone":
        return _DEATH_VALUE

    if effect_type == "destroy":
        return _HANGED_MAN_EARLY if ante <= _HANGED_MAN_ANTE_CUTOFF else _HANGED_MAN_LATE

    if effect_type == "glass":
        base = _JUSTICE_VALUE
        jokers = state.get("jokers", {}).get("cards", [])
        if any(joker_key(j) == "j_glass" for j in jokers):
            base += _GLASS_JOKER_BONUS
        return base

    if effect_type == "suit_convert":
        suit_aff = strat.suit_affinity(extra) if strat and extra else 0.0
        if suit_aff <= 0:
            return _SUIT_CONVERT_NO_AFFINITY
        return 2.0 + suit_aff

    if effect_type == "enhance":
        base = _ENHANCE_SCORES.get(extra, 2.0)
        jokers = state.get("jokers", {}).get("cards", [])
        joker_keys = {joker_key(j) for j in jokers}
        if extra == "Lucky":
            if "j_lucky_cat" in joker_keys:
                base += _LUCKY_CAT_BONUS
            if "j_oops" in joker_keys:
                base += _OOPS_BONUS
        elif extra == "Steel":
            if "j_steel_joker" in joker_keys:
                base += _STEEL_JOKER_BONUS
        elif extra == "Glass":
            if "j_glass" in joker_keys:
                base += _GLASS_JOKER_BONUS
        elif extra == "Gold":
            if "j_golden" in joker_keys:
                base += _GOLD_JOKER_BONUS
        if ante <= _EARLY_ANTE_CUTOFF:
            base *= _EARLY_ANTE_ENHANCE_BONUS
        return base

    if effect_type == "gold":
        base = remaining_rounds * _DEVIL_INCOME_PER_ROUND / _DEVIL_DIVISOR
        jokers = state.get("jokers", {}).get("cards", [])
        if any(joker_key(j) == "j_golden" for j in jokers):
            base += _GOLD_JOKER_BONUS
        return base

    if effect_type == "stone":
        has_stone_joker = any(joker_key(j) == "j_stone"
                             for j in state.get("jokers", {}).get("cards", []))
        return _STONE_WITH_JOKER if has_stone_joker else _STONE_WITHOUT_JOKER

    if effect_type == "rank_up":
        return _RANK_UP_VALUE

    return _TARGETING_FALLBACK


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
        return _HEX_SOLO_VALUE  # 1 joker: free Polychrome

    owned_keys = {joker_key(j) for j in jokers}
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
    if best_val < _HEX_MIN_BEST_VAL:
        return 0.0  # best joker not strong enough to solo
    if rest_total > _HEX_POLYCHROME_VALUE + _HEX_MARGIN:
        return 0.0  # sacrificing too much

    # Scale score by how dominant the best joker is
    dominance = best_val / max(rest_total, 1.0)
    score = _HEX_BASE_SCORE + dominance

    # Late-game soft penalty: harder to rebuild, need a truly dominant joker
    for min_ante, penalty in sorted(_HEX_ANTE_PENALTIES.items(), reverse=True):
        if ante >= min_ante:
            score *= penalty
            break

    return min(score, _HEX_MAX_SCORE)


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
        return (_PLANET_USE_VALUE, ("use", idx,
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
        if key == "c_high_priestess":
            # Creates up to 2 planets. After use its own slot frees, so full
            # output needs ≥1 other free slot going in. If slots are full and
            # another consumable is held, hold Priestess — the other one will
            # likely fire this tick or next and free a slot for us.
            cons = state.get("consumables", {})
            count = cons.get("count", len(cons.get("cards", [])))
            limit = cons.get("limit", 2)
            if count >= limit:
                others = [c for c in cons.get("cards", [])
                          if c.get("key") != "c_high_priestess"]
                if others:
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
        return (_SAFE_SPECTRAL_USE_VALUE, ("use", idx, f"use spectral: {label}"))

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
            threshold = _DESPERATE_MIN_IMPROVEMENT if desperate else _SUIT_CONVERT_IMPROVEMENT
            if improvement > threshold:
                return (improvement * _TACTICAL_USE_MULTIPLIER, ("use_target", idx, targets,
                    f"tactical: {label} -> Flush ({new_score} vs {current_score}, {hands_left}h left)"))
        return (0.0, None)

    # Enhancements and Glass: use based on ante timing
    if effect_type in ("glass", "enhance"):
        if ante <= _EARLY_ANTE_CUTOFF:
            threshold = _GLASS_ANTE_THRESHOLDS["early"]
        elif ante <= 5:
            threshold = _GLASS_ANTE_THRESHOLDS["mid"]
        else:
            if not desperate:
                return (0.0, None)
            threshold = _GLASS_ANTE_THRESHOLDS["late"]

        effective_threshold = max(threshold, _DESPERATE_MIN_IMPROVEMENT) if desperate else threshold
        if effect_type == "glass":
            result = eval_glass(hand_cards, hand_levels, jokers, current_best, current_score, strat=strat)
            if result:
                new_score, targets = result
                improvement = new_score / max(current_score, 1)
                if improvement >= effective_threshold:
                    return (improvement * _TACTICAL_USE_MULTIPLIER, ("use_target", idx, targets,
                        f"{'desperate' if desperate else 'tactical'}: Glass (×{improvement:.1f}, {hands_left}h left)"))
        else:
            result = eval_enhancement(
                hand_cards, hand_levels, jokers, current_best, current_score,
                extra, max_count, strat=strat,
            )
            if result:
                new_score, targets = result
                improvement = new_score / max(current_score, 1)
                if improvement >= effective_threshold:
                    return (improvement * _TACTICAL_USE_MULTIPLIER, ("use_target", idx, targets,
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
            hold = _HOLD_SUIT_CONVERT_WITH_DISCARDS if discards_left > 0 else _HOLD_SUIT_CONVERT_NO_DISCARDS

        elif effect_type in ("glass", "enhance"):
            # Early: low hold value (use freely, compound)
            # Late: high hold value (wait for best target)
            if ante <= _EARLY_ANTE_CUTOFF:
                hold = _HOLD_ENHANCE_EARLY
            elif ante <= 5:
                hold = _HOLD_ENHANCE_MID
            else:
                hold = _HOLD_ENHANCE_LATE

        elif effect_type == "gold":
            hold = _HOLD_GOLD

        else:
            # Immediate-use tarots (destroy, clone, etc)
            hold = 0.0

    # Slot pressure: holding a mediocre card blocks better purchases
    if slots_full:
        hold -= _HOLD_SLOT_PRESSURE

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
    multiplier = _GLASS_MULTIPLIER_FACE if is_face else _GLASS_MULTIPLIER_OTHER
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
                    estimated = int(current_score * _WILD_STEEL_MULTIPLIER)
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
            estimated = int(current_score * _WILD_STEEL_MULTIPLIER)
            return (estimated, held[:max_count])
        return None

    # Other enhancements: apply to highest-value scoring card, prefer affinity ranks
    targets = _find_enhancement_targets(hand_cards, max_count, rank_affinity=rank_aff)
    if targets:
        scoring_set = set(current_best.card_indices)
        relevant = [t for t in targets if t in scoring_set]
        if relevant:
            estimated = int(current_score * _GENERIC_ENHANCE_MULTIPLIER)
            return (estimated, relevant[:max_count])

    return None
