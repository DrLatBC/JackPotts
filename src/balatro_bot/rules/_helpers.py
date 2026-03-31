"""Shared helper functions for rules."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from balatro_bot.cards import card_rank, card_suit, card_suits, card_xmult_value, rank_value, is_debuffed, _modifier
from balatro_bot.constants import (
    FEWER_CARDS_JOKERS, ALL_SCORE_JOKERS, EXACT_4_JOKERS,
    FACE_RANKS_TAROT, PLANET_KEYS, NO_TARGET_TAROTS, TARGETING_TAROTS,
    SCALING_JOKERS,
)
from balatro_bot.hand_evaluator import classify_hand

if TYPE_CHECKING:
    from typing import Any
    from balatro_bot.strategy import Strategy

log = logging.getLogger("balatro_bot")


def _pad_with_junk(
    card_indices: list[int],
    hand_cards: list[dict],
    jokers: list[dict],
    intended_hand: str = "",
    max_cards: int = 5,
) -> list[int]:
    """Pad a hand with low-value junk cards for free deck cycling.

    If intended_hand is provided, each candidate junk card is checked to
    ensure it doesn't change the hand classification (e.g. turning a High
    Card into a Pair, or creating an accidental Flush/Straight).  If we
    can't fill to target without changing the hand, we play fewer cards.
    """
    joker_keys = {j.get("key") for j in jokers}
    n = len(card_indices)

    if joker_keys & FEWER_CARDS_JOKERS and n <= 3:
        return card_indices
    if joker_keys & ALL_SCORE_JOKERS:
        return card_indices

    if joker_keys & EXACT_4_JOKERS:
        target = 4
    else:
        target = max_cards

    if n >= target:
        return card_indices

    four_fingers = "j_four_fingers" in joker_keys
    shortcut = "j_shortcut" in joker_keys
    smeared = "j_smeared" in joker_keys

    used = set(card_indices)
    junk = []
    for i, c in enumerate(hand_cards):
        if i not in used:
            r = card_rank(c)
            debuffed = is_debuffed(c)
            junk.append((i, 0 if debuffed else 1, rank_value(r) if r else 0))
    junk.sort(key=lambda x: (x[1], x[2]))

    padded = list(card_indices)
    for i, _, _ in junk:
        if len(padded) >= target:
            break
        if intended_hand:
            test_cards = [hand_cards[j] for j in padded] + [hand_cards[i]]
            if classify_hand(test_cards, four_fingers, shortcut, smeared) != intended_hand:
                continue
        padded.append(i)

    return padded


def _sort_play_order(
    indices: list[int],
    hand_cards: list[dict],
    jokers: list[dict],
) -> list[int]:
    """Sort card play indices so additive effects fire before multiplicative.

    In Balatro, played cards score left-to-right. Cards with ×mult (Glass,
    Polychrome) should be rightmost so all +chips/+mult accumulate first.

    With Hanging Chad, the first card gets +2 retriggers (3 total). The card
    that benefits most from compounded retriggers goes first instead.
    """
    if len(indices) <= 1:
        return indices

    joker_keys = {j.get("key") for j in jokers}
    has_hanging_chad = "j_hanging_chad" in joker_keys
    has_photograph = "j_photograph" in joker_keys
    has_triboulet = "j_triboulet" in joker_keys
    pareidolia = "j_pareidolia" in joker_keys

    def _total_xmult(idx: int) -> float:
        """Per-trigger xmult for this card (card ×mult + per-card joker ×mult)."""
        c = hand_cards[idx]
        if is_debuffed(c):
            return 1.0
        xm = card_xmult_value(c)
        rank = card_rank(c)
        if has_triboulet and rank in ("K", "Q"):
            xm *= 2.0
        return xm

    if has_hanging_chad:
        # First card gets 3 triggers — pick the card that benefits most.
        def _first_position_score(idx: int) -> float:
            c = hand_cards[idx]
            if is_debuffed(c):
                return 0.0
            xm = _total_xmult(idx)
            rank = card_rank(c)
            is_face = pareidolia or rank in ("J", "Q", "K")
            if has_photograph and is_face:
                xm *= 2.0  # Photograph ×2 per trigger on first face
            # xm^3 (first) vs xm (elsewhere) — higher ratio = more benefit
            return xm ** 3

        best_first = max(indices, key=_first_position_score)
        rest = [i for i in indices if i != best_first]
        # Sort rest: additive left (xmult<=1), multiplicative right (xmult>1)
        rest.sort(key=lambda i: (0 if card_xmult_value(hand_cards[i]) <= 1.0 else 1, i))
        return [best_first] + rest

    # No Hanging Chad: simple sort — additive left, ×mult right
    return sorted(indices, key=lambda i: (
        0 if card_xmult_value(hand_cards[i]) <= 1.0 else 1,
        i,  # stability tiebreak: preserve original order within group
    ))


def _find_gold_targets(hand_cards, count, current_best=None):
    best_indices = set(current_best.card_indices) if current_best else set()
    candidates = []
    for i, c in enumerate(hand_cards):
        if i in best_indices:
            continue
        if is_debuffed(c):
            continue
        if _modifier(c).get("enhancement"):
            continue
        r = card_rank(c)
        candidates.append((i, rank_value(r) if r else 0))
    candidates.sort(key=lambda x: x[1])
    return [i for i, _ in candidates[:count]]


def _find_enhancement_targets(hand_cards, count, rank_affinity=None):
    candidates = []
    for i, c in enumerate(hand_cards):
        r = card_rank(c)
        if r and not _modifier(c).get("enhancement"):
            aff = rank_affinity.get(r, 0.0) if rank_affinity else 0.0
            candidates.append((i, -aff, -rank_value(r)))  # high affinity first, then high rank
    candidates.sort(key=lambda x: (x[1], x[2]))
    return [i for i, _, _ in candidates[:count]]


def _find_suit_convert_targets(hand_cards, target_suit, count):
    candidates = []
    for i, c in enumerate(hand_cards):
        s = card_suit(c)
        if s and s != target_suit:
            candidates.append(i)
    return candidates[:count]


def _find_rank_up_targets(hand_cards, count, rank_affinity=None):
    candidates = []
    for i, c in enumerate(hand_cards):
        r = card_rank(c)
        if r:
            aff = rank_affinity.get(r, 0.0) if rank_affinity else 0.0
            candidates.append((i, aff, rank_value(r)))  # low affinity first (don't upgrade away)
    candidates.sort(key=lambda x: (x[1], x[2]))
    return [i for i, _, _ in candidates[:count]]


def _find_glass_targets(hand_cards, count, rank_affinity=None):
    candidates = []
    for i, c in enumerate(hand_cards):
        r = card_rank(c)
        if r and not _modifier(c).get("enhancement"):
            aff = rank_affinity.get(r, 0.0) if rank_affinity else 0.0
            is_face = r in FACE_RANKS_TAROT
            candidates.append((i, 0 if is_face else 1, -aff, -rank_value(r)))
    candidates.sort(key=lambda x: (x[1], x[2], x[3]))
    return [i for i, _, _, _ in candidates[:count]]


def _find_stone_targets(hand_cards, count, jokers, rank_affinity=None):
    has_stone_joker = any(j.get("key") == "j_stone" for j in jokers)
    candidates = []
    for i, c in enumerate(hand_cards):
        r = card_rank(c)
        if r and not _modifier(c).get("enhancement"):
            rv = rank_value(r)
            if has_stone_joker or rv <= 4:
                aff = rank_affinity.get(r, 0.0) if rank_affinity else 0.0
                candidates.append((i, aff, rv))  # low affinity first
    candidates.sort(key=lambda x: (x[1], x[2]))
    return [i for i, _, _ in candidates[:count]]


def _find_destroy_targets(hand_cards, count, rank_affinity=None):
    candidates = []
    for i, c in enumerate(hand_cards):
        r = card_rank(c)
        if r and not _modifier(c).get("enhancement"):
            aff = rank_affinity.get(r, 0.0) if rank_affinity else 0.0
            candidates.append((i, aff, rank_value(r)))  # low/negative affinity first
    candidates.sort(key=lambda x: (x[1], x[2]))
    return [i for i, _, _ in candidates[:count]]


def _find_clone_targets(hand_cards, strategy=None):
    scored = []
    for i, c in enumerate(hand_cards):
        r = card_rank(c)
        if not r or is_debuffed(c):
            continue
        score = rank_value(r)
        mod = _modifier(c)
        enh = mod.get("enhancement")
        if enh == "STEEL":
            score += 15
        elif enh == "GLASS":
            score += 10
        elif enh in ("MULT", "LUCKY"):
            score += 5
        elif enh == "WILD":
            score += 4
        elif enh == "BONUS":
            score += 3
        elif enh == "GOLD":
            score += 2
        if mod.get("seal"):
            score += 8
        edition = mod.get("edition")
        if edition == "POLYCHROME":
            score += 20
        elif edition == "HOLOGRAPHIC":
            score += 10
        elif edition == "FOIL":
            score += 5
        if strategy:
            s = card_suit(c)
            if s:
                score += strategy.suit_affinity(s) * 3
            score += strategy.rank_affinity(r) * 2
        scored.append((i, score))
    if len(scored) < 2:
        return None
    scored.sort(key=lambda x: x[1])
    worst_idx, worst_sc = scored[0]
    best_idx, best_sc = scored[-1]
    if best_sc - worst_sc < 4:
        return None
    return (best_idx, worst_idx)


def _find_seal_targets(hand_cards, count):
    candidates = []
    for i, c in enumerate(hand_cards):
        r = card_rank(c)
        if r and not _modifier(c).get("seal"):
            candidates.append((i, rank_value(r)))
    candidates.sort(key=lambda x: -x[1])
    return [i for i, _ in candidates[:count]]


def _find_edition_targets(hand_cards, count):
    candidates = []
    for i, c in enumerate(hand_cards):
        r = card_rank(c)
        if r and not _modifier(c).get("edition"):
            candidates.append((i, rank_value(r)))
    candidates.sort(key=lambda x: -x[1])
    return [i for i, _ in candidates[:count]]


def _find_deck_enhance_targets(hand_cards, count):
    candidates = []
    for i, c in enumerate(hand_cards):
        r = card_rank(c)
        if r and not _modifier(c).get("enhancement"):
            candidates.append((i, rank_value(r)))
    candidates.sort(key=lambda x: x[1])
    return [i for i, _ in candidates[:count]]


def _find_clone_deck_targets(hand_cards, count, current_best):
    if not current_best:
        return []
    scoring_set = set(current_best.card_indices)
    candidates = []
    for i, c in enumerate(hand_cards):
        if i not in scoring_set:
            continue
        r = card_rank(c)
        if r:
            candidates.append((i, rank_value(r)))
    candidates.sort(key=lambda x: -x[1])
    return [i for i, _ in candidates[:count]]


def _find_tarot_targets(effect_type, extra, max_count, hand_cards, jokers, strat, current_best=None):
    rank_aff = strat.rank_affinity_dict() if strat else None
    if effect_type == "gold":
        targets = _find_gold_targets(hand_cards, max_count, current_best)
        return (targets, 4.0) if targets else (None, 0)
    if effect_type == "enhance":
        targets = _find_enhancement_targets(hand_cards, max_count, rank_affinity=rank_aff)
        return (targets, 3.0) if targets else (None, 0)
    if effect_type == "suit_convert":
        if not extra or strat.suit_affinity(extra) <= 0:
            return (None, 0)
        targets = _find_suit_convert_targets(hand_cards, extra, max_count)
        return (targets, 2.0 + strat.suit_affinity(extra)) if targets else (None, 0)
    if effect_type == "rank_up":
        targets = _find_rank_up_targets(hand_cards, max_count, rank_affinity=rank_aff)
        return (targets, 2.0) if targets else (None, 0)
    if effect_type == "glass":
        targets = _find_glass_targets(hand_cards, max_count, rank_affinity=rank_aff)
        return (targets, 4.0) if targets else (None, 0)
    if effect_type == "stone":
        targets = _find_stone_targets(hand_cards, max_count, jokers, rank_affinity=rank_aff)
        return (targets, 1.5) if targets else (None, 0)
    if effect_type == "destroy":
        targets = _find_destroy_targets(hand_cards, max_count, rank_affinity=rank_aff)
        return (targets, 1.0) if targets else (None, 0)
    if effect_type == "clone":
        targets = _find_clone_targets(hand_cards, strat)
        return (targets, 3.5) if targets else (None, 0)
    if effect_type == "deck_enhance":
        targets = _find_deck_enhance_targets(hand_cards, max_count)
        return (targets, 2.0) if targets else (None, 0)
    if effect_type == "seal":
        targets = _find_seal_targets(hand_cards, max_count)
        return (targets, 2.0) if targets else (None, 0)
    if effect_type == "edition":
        targets = _find_edition_targets(hand_cards, max_count)
        return (targets, 1.0) if targets else (None, 0)
    if effect_type == "clone_deck":
        targets = _find_clone_deck_targets(hand_cards, max_count, current_best)
        return (targets, 1.5) if targets else (None, 0)
    return (None, 0)


# ---------------------------------------------------------------------------
# Dynamic consumable value scoring
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
        n_jokers = len(jokers)
        return 3.0 if n_jokers >= 3 else (1.5 if n_jokers >= 1 else 0.0)
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
