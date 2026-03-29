"""Shared helper functions for rules."""

from __future__ import annotations

from typing import TYPE_CHECKING

from balatro_bot.cards import card_rank, card_suit, card_suits, rank_value, is_debuffed, _modifier
from balatro_bot.constants import (
    FEWER_CARDS_JOKERS, ALL_SCORE_JOKERS, EXACT_4_JOKERS,
    FACE_RANKS_TAROT,
)

if TYPE_CHECKING:
    from typing import Any


def _pad_with_junk(
    card_indices: list[int],
    hand_cards: list[dict],
    jokers: list[dict],
    max_cards: int = 5,
) -> list[int]:
    """Pad a hand with low-value junk cards for free deck cycling."""
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
        padded.append(i)

    return padded


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


def _find_enhancement_targets(hand_cards, count):
    candidates = []
    for i, c in enumerate(hand_cards):
        r = card_rank(c)
        if r and not _modifier(c).get("enhancement"):
            candidates.append((i, rank_value(r)))
    candidates.sort(key=lambda x: -x[1])
    return [i for i, _ in candidates[:count]]


def _find_suit_convert_targets(hand_cards, target_suit, count):
    candidates = []
    for i, c in enumerate(hand_cards):
        s = card_suit(c)
        if s and s != target_suit:
            candidates.append(i)
    return candidates[:count]


def _find_rank_up_targets(hand_cards, count):
    candidates = []
    for i, c in enumerate(hand_cards):
        r = card_rank(c)
        if r:
            candidates.append((i, rank_value(r)))
    candidates.sort(key=lambda x: x[1])
    return [i for i, _ in candidates[:count]]


def _find_glass_targets(hand_cards, count):
    candidates = []
    for i, c in enumerate(hand_cards):
        r = card_rank(c)
        if r and not _modifier(c).get("enhancement"):
            is_face = r in FACE_RANKS_TAROT
            candidates.append((i, 0 if is_face else 1, -rank_value(r)))
    candidates.sort(key=lambda x: (x[1], x[2]))
    return [i for i, _, _ in candidates[:count]]


def _find_stone_targets(hand_cards, count, jokers):
    has_stone_joker = any(j.get("key") == "j_stone" for j in jokers)
    candidates = []
    for i, c in enumerate(hand_cards):
        r = card_rank(c)
        if r and not _modifier(c).get("enhancement"):
            rv = rank_value(r)
            if has_stone_joker or rv <= 4:
                candidates.append((i, rv))
    candidates.sort(key=lambda x: x[1])
    return [i for i, _ in candidates[:count]]


def _find_destroy_targets(hand_cards, count):
    candidates = []
    for i, c in enumerate(hand_cards):
        r = card_rank(c)
        if r and not _modifier(c).get("enhancement"):
            candidates.append((i, rank_value(r)))
    candidates.sort(key=lambda x: x[1])
    return [i for i, _ in candidates[:count]]


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
        scored.append((i, score))
    if len(scored) < 2:
        return None
    scored.sort(key=lambda x: x[1])
    worst_idx, worst_sc = scored[0]
    best_idx, best_sc = scored[-1]
    if best_sc - worst_sc < 4:
        return None
    if worst_idx < best_idx:
        return [worst_idx, best_idx]
    for idx, sc in scored:
        if idx < best_idx and best_sc - sc >= 4:
            return [idx, best_idx]
    for idx, sc in reversed(scored):
        if idx > worst_idx and sc - worst_sc >= 4:
            return [worst_idx, idx]
    return None


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
    if effect_type == "gold":
        targets = _find_gold_targets(hand_cards, max_count, current_best)
        return (targets, 4.0) if targets else (None, 0)
    if effect_type == "enhance":
        targets = _find_enhancement_targets(hand_cards, max_count)
        return (targets, 3.0) if targets else (None, 0)
    if effect_type == "suit_convert":
        if not extra or strat.suit_affinity(extra) <= 0:
            return (None, 0)
        targets = _find_suit_convert_targets(hand_cards, extra, max_count)
        return (targets, 2.0 + strat.suit_affinity(extra)) if targets else (None, 0)
    if effect_type == "rank_up":
        targets = _find_rank_up_targets(hand_cards, max_count)
        return (targets, 2.0) if targets else (None, 0)
    if effect_type == "glass":
        targets = _find_glass_targets(hand_cards, max_count)
        return (targets, 4.0) if targets else (None, 0)
    if effect_type == "stone":
        targets = _find_stone_targets(hand_cards, max_count, jokers)
        return (targets, 1.5) if targets else (None, 0)
    if effect_type == "destroy":
        targets = _find_destroy_targets(hand_cards, max_count)
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
