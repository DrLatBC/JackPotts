"""Shared helper functions for rules."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from balatro_bot.cards import card_rank, card_suit, card_suits, card_xmult_value, card_chip_value, card_mult_value, joker_key, rank_value, is_debuffed, _modifier
from balatro_bot.constants import (
    FEWER_CARDS_JOKERS, ALL_SCORE_JOKERS, EXACT_4_JOKERS,
    FACE_RANKS, FACE_RANKS_TAROT, PLANET_KEYS, NO_TARGET_TAROTS, TARGETING_TAROTS,
    SCALING_JOKERS, EVEN_RANKS, ODD_RANKS, FIBONACCI_RANKS,
)
from balatro_bot.domain.scoring.classify import classify_hand

if TYPE_CHECKING:
    from typing import Any
    from balatro_bot.strategy import CardProtection, Strategy

log = logging.getLogger("balatro_bot")


def _pad_with_junk(
    card_indices: list[int],
    hand_cards: list[dict],
    jokers: list[dict],
    intended_hand: str = "",
    max_cards: int = 5,
    strategy: Strategy | None = None,
    scoring_suit: str | None = None,
    protection: CardProtection | None = None,
) -> list[int]:
    """Pad a hand with junk cards for free deck cycling.

    Uses the same strategic discard ordering as the discard system —
    cards we'd most want to discard are the first ones cycled out via play.
    If intended_hand is provided, each candidate is checked to ensure it
    doesn't change the hand classification.
    """
    from balatro_bot.domain.scoring.search import cards_not_in

    jk = {joker_key(j) for j in jokers}
    n = len(card_indices)

    if jk & FEWER_CARDS_JOKERS and n <= 3:
        return card_indices
    if jk & ALL_SCORE_JOKERS:
        return card_indices

    if jk & EXACT_4_JOKERS:
        target = 4
    else:
        target = max_cards

    if n >= target:
        return card_indices

    four_fingers = "j_four_fingers" in jk
    shortcut = "j_shortcut" in jk
    smeared = "j_smeared" in jk

    if protection is None and strategy is not None:
        protection = strategy.card_protection(jokers=jokers, scoring_suit=scoring_suit)

    # cards_not_in returns non-keep indices sorted worst-first (discard priority)
    junk_priority = cards_not_in(
        hand_cards, set(card_indices),
        protection=protection,
    )

    # For High Card, junk must not outrank the scoring card or it becomes
    # the new scoring card (game picks highest rank, leftmost on ties).
    max_junk_rank: int | None = None
    if intended_hand == "High Card" and card_indices:
        scoring_rank = rank_value(card_rank(hand_cards[card_indices[0]]) or "2")
        max_junk_rank = scoring_rank

    padded = list(card_indices)
    for i in junk_priority:
        if len(padded) >= target:
            break
        if max_junk_rank is not None:
            rv = rank_value(card_rank(hand_cards[i]) or "2")
            if rv > max_junk_rank:
                continue
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
    strategy: "Strategy | None" = None,
) -> list[int]:
    """Sort card play indices so additive effects fire before multiplicative.

    In Balatro, played cards score left-to-right. Cards with ×mult (Glass,
    Polychrome) should be rightmost so all +chips/+mult accumulate first.

    With Hanging Chad, the first card gets +2 retriggers (3 total). The card
    that benefits most from compounded retriggers goes first instead.
    """
    if len(indices) <= 1:
        return indices

    joker_keys = {joker_key(j) for j in jokers}
    has_hanging_chad = "j_hanging_chad" in joker_keys
    has_photograph = "j_photograph" in joker_keys
    has_triboulet = "j_triboulet" in joker_keys
    has_dna = "j_dna" in joker_keys
    pareidolia = "j_pareidolia" in joker_keys

    def _is_face(idx: int) -> bool:
        return pareidolia or card_rank(hand_cards[idx]) in ("J", "Q", "K")

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

    def _sort_rest(rest: list[int]) -> list[int]:
        """Sort non-first cards: additive left, multiplicative right."""
        rest.sort(key=lambda i: (0 if card_xmult_value(hand_cards[i]) <= 1.0 else 1, i))
        return rest

    if has_hanging_chad:
        # First card gets 3 triggers — pick the card that benefits most.
        def _per_trigger_add(idx: int) -> float:
            """Additive chips+mult this card earns per trigger from joker effects."""
            c = hand_cards[idx]
            if is_debuffed(c):
                return 0.0
            rank = card_rank(c)
            suits = card_suits(c)
            add = card_chip_value(c) + card_mult_value(c)
            for j in jokers:
                k = joker_key(j)
                if k == "j_even_steven" and rank in EVEN_RANKS:
                    add += 4
                elif k == "j_odd_todd" and rank in ODD_RANKS:
                    add += 31
                elif k == "j_fibonacci" and rank in FIBONACCI_RANKS:
                    add += 8
                elif k == "j_smiley" and (pareidolia or rank in FACE_RANKS):
                    add += 5
                elif k == "j_scary_face" and (pareidolia or rank in FACE_RANKS):
                    add += 30
                elif k == "j_scholar" and rank == "A":
                    add += 24  # 20 chips + 4 mult
                elif k == "j_walkie_talkie" and rank in ("T", "4"):
                    add += 14  # 10 chips + 4 mult
                elif k == "j_greedy_joker" and "D" in suits:
                    add += 3
                elif k == "j_lusty_joker" and "H" in suits:
                    add += 3
                elif k == "j_wrathful_joker" and "S" in suits:
                    add += 3
                elif k == "j_gluttenous_joker" and "C" in suits:
                    add += 3
                elif k == "j_arrowhead" and "S" in suits:
                    add += 50
                elif k == "j_onyx_agate" and "C" in suits:
                    add += 7
            return add

        def _first_position_score(idx: int) -> float:
            c = hand_cards[idx]
            if is_debuffed(c):
                return 0.0
            xm = _total_xmult(idx)
            if has_photograph and _is_face(idx):
                xm *= 2.0  # Photograph ×2 per trigger on first face
            add = _per_trigger_add(idx)
            # 3 triggers in first position: xm^3 for multiplicative,
            # 3*add vs 1*add = 2*add extra for additive
            return xm ** 3 + 2 * add

        best_first = max(indices, key=_first_position_score)
        rest = [i for i in indices if i != best_first]
        return [best_first] + _sort_rest(rest)

    # Photograph without Hanging Chad: put best face card first for xmult
    if has_photograph:
        face_indices = [i for i in indices if not is_debuffed(hand_cards[i]) and _is_face(i)]
        if face_indices:
            best_face = max(face_indices, key=_total_xmult)
            rest = [i for i in indices if i != best_face]
            return [best_face] + _sort_rest(rest)

    # DNA: first card played gets copied to deck — pick the most strategic card
    if has_dna:
        def _dna_value(idx: int) -> float:
            c = hand_cards[idx]
            if is_debuffed(c):
                return -1.0
            rank = card_rank(c)
            suit = card_suit(c)
            score = 0.0
            if strategy:
                score += strategy.rank_affinity(rank) if rank else 0.0
                score += strategy.suit_affinity(suit) if suit else 0.0
            # Fallback: prefer high chip-value cards when no strategy signal
            if score == 0.0:
                score = card_chip_value(c) / 100.0
            return score

        best_dna = max(indices, key=_dna_value)
        rest = [i for i in indices if i != best_dna]
        return [best_dna] + _sort_rest(rest)

    # Default: additive left, ×mult right
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


def _find_suit_convert_targets(hand_cards, target_suit, count, rank_affinity=None):
    candidates = []
    for i, c in enumerate(hand_cards):
        s = card_suit(c)
        if s and s != target_suit:
            r = card_rank(c)
            aff = rank_affinity.get(r, 0.0) if rank_affinity else 0.0
            candidates.append((i, aff))
    candidates.sort(key=lambda x: x[1])  # low affinity first = convert first
    return [i for i, _ in candidates[:count]]


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
    has_stone_joker = any(joker_key(j) == "j_stone" for j in jokers)
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


def _find_destroy_targets(hand_cards, count, rank_affinity=None, strategy=None):
    candidates = []
    for i, c in enumerate(hand_cards):
        r = card_rank(c)
        if r and not _modifier(c).get("enhancement"):
            # Base score from rank affinity (low affinity → destroy first)
            score = rank_affinity.get(r, 0.0) if rank_affinity else 0.0

            # Suit-awareness: off-suit cards in suit builds are prime targets
            if strategy and strategy.preferred_suits:
                s = card_suit(c)
                if s:
                    suit_aff = strategy.suit_affinity(s)
                    if suit_aff > 0:
                        score += 3.0   # protect on-suit cards
                    elif suit_aff <= 0:
                        score -= 2.0   # target off-suit cards

            candidates.append((i, score, rank_value(r)))
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
        targets = _find_suit_convert_targets(hand_cards, extra, max_count, rank_affinity=rank_aff)
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
        targets = _find_destroy_targets(hand_cards, max_count, rank_affinity=rank_aff,
                                        strategy=strat)
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
# Dynamic consumable value scoring — canonical home is consumable_policy.py
# Re-exported here for backward compatibility.
# ---------------------------------------------------------------------------

from balatro_bot.domain.policy.consumable_policy import (  # noqa: F401
    score_consumable,
    evaluate_hex,
)
