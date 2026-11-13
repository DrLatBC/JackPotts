"""Which hand cards a tarot, spectral or seal effect should target."""

from __future__ import annotations

from balatro_bot.cards import card_rank, card_suit, joker_key, rank_value, is_debuffed, _modifier
from balatro_bot.constants import FACE_RANKS_TAROT


def find_gold_targets(hand_cards, count, current_best=None):
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


def find_enhancement_targets(hand_cards, count, rank_affinity=None):
    candidates = []
    for i, c in enumerate(hand_cards):
        r = card_rank(c)
        if r and not _modifier(c).get("enhancement"):
            aff = rank_affinity.get(r, 0.0) if rank_affinity else 0.0
            candidates.append((i, -aff, -rank_value(r)))  # high affinity first, then high rank
    candidates.sort(key=lambda x: (x[1], x[2]))
    return [i for i, _, _ in candidates[:count]]


def find_suit_convert_targets(hand_cards, target_suit, count, rank_affinity=None):
    candidates = []
    for i, c in enumerate(hand_cards):
        s = card_suit(c)
        if s and s != target_suit:
            r = card_rank(c)
            aff = rank_affinity.get(r, 0.0) if rank_affinity else 0.0
            candidates.append((i, aff))
    candidates.sort(key=lambda x: x[1])  # low affinity first = convert first
    return [i for i, _ in candidates[:count]]


def find_rank_up_targets(hand_cards, count, rank_affinity=None):
    candidates = []
    for i, c in enumerate(hand_cards):
        r = card_rank(c)
        if r:
            aff = rank_affinity.get(r, 0.0) if rank_affinity else 0.0
            candidates.append((i, aff, rank_value(r)))  # low affinity first (don't upgrade away)
    candidates.sort(key=lambda x: (x[1], x[2]))
    return [i for i, _, _ in candidates[:count]]


def find_glass_targets(hand_cards, count, rank_affinity=None):
    candidates = []
    for i, c in enumerate(hand_cards):
        r = card_rank(c)
        if r and not _modifier(c).get("enhancement"):
            aff = rank_affinity.get(r, 0.0) if rank_affinity else 0.0
            is_face = r in FACE_RANKS_TAROT
            candidates.append((i, 0 if is_face else 1, -aff, -rank_value(r)))
    candidates.sort(key=lambda x: (x[1], x[2], x[3]))
    return [i for i, _, _, _ in candidates[:count]]


def find_stone_targets(hand_cards, count, jokers, rank_affinity=None):
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


def find_destroy_targets(hand_cards, count, rank_affinity=None, strategy=None):
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


def find_clone_targets(hand_cards, strategy=None):
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


def find_seal_targets(hand_cards, count, seal_type=None, strategy=None, current_best=None):
    """Pick targets for a seal-applying consumable.

    Red/Gold: best scoring card (plays often, compounds). Prefer on-strategy.
    Blue: card that will be HELD not played — off-strategy / low rank.
    Purple: card to discard — off-strategy / low rank.
    """
    seal = (seal_type or "").upper()
    prefer_scoring = seal in ("RED", "GOLD")
    scoring_set = set(current_best.card_indices) if current_best else set()
    rank_aff = strategy.rank_affinity_dict() if strategy else {}
    suit_aff = dict(strategy.preferred_suits) if strategy else {}

    candidates = []
    for i, c in enumerate(hand_cards):
        r = card_rank(c)
        if not r or _modifier(c).get("seal"):
            continue
        rv = rank_value(r)
        aff = rank_aff.get(r, 0.0)
        s = card_suit(c)
        if s:
            aff += suit_aff.get(s, 0.0)
        in_scoring = 1 if i in scoring_set else 0
        if prefer_scoring:
            # Rank high, on-strategy, in current scoring set all boost
            score = rv + aff * 2.0 + in_scoring * 5.0
            candidates.append((i, -score))
        else:
            # Off-strategy, low rank, not in scoring set all boost (we want to keep/discard this one)
            score = rv + aff * 2.0 + in_scoring * 5.0
            candidates.append((i, score))
    candidates.sort(key=lambda x: x[1])
    return [i for i, _ in candidates[:count]]


def find_edition_targets(hand_cards, count):
    candidates = []
    for i, c in enumerate(hand_cards):
        r = card_rank(c)
        if r and not _modifier(c).get("edition"):
            candidates.append((i, rank_value(r)))
    candidates.sort(key=lambda x: -x[1])
    return [i for i, _ in candidates[:count]]


def find_deck_enhance_targets(hand_cards, count):
    candidates = []
    for i, c in enumerate(hand_cards):
        r = card_rank(c)
        if r and not _modifier(c).get("enhancement"):
            candidates.append((i, rank_value(r)))
    candidates.sort(key=lambda x: x[1])
    return [i for i, _ in candidates[:count]]


def find_clone_deck_targets(hand_cards, count, current_best):
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


def find_tarot_targets(effect_type, extra, max_count, hand_cards, jokers, strat, current_best=None):
    rank_aff = strat.rank_affinity_dict() if strat else None
    if effect_type == "gold":
        targets = find_gold_targets(hand_cards, max_count, current_best)
        return (targets, 4.0) if targets else (None, 0)
    if effect_type == "enhance":
        targets = find_enhancement_targets(hand_cards, max_count, rank_affinity=rank_aff)
        return (targets, 3.0) if targets else (None, 0)
    if effect_type == "suit_convert":
        if not extra or strat.suit_affinity(extra) <= 0:
            return (None, 0)
        targets = find_suit_convert_targets(hand_cards, extra, max_count, rank_affinity=rank_aff)
        return (targets, 2.0 + strat.suit_affinity(extra)) if targets else (None, 0)
    if effect_type == "rank_up":
        targets = find_rank_up_targets(hand_cards, max_count, rank_affinity=rank_aff)
        return (targets, 2.0) if targets else (None, 0)
    if effect_type == "glass":
        targets = find_glass_targets(hand_cards, max_count, rank_affinity=rank_aff)
        return (targets, 4.0) if targets else (None, 0)
    if effect_type == "stone":
        targets = find_stone_targets(hand_cards, max_count, jokers, rank_affinity=rank_aff)
        return (targets, 1.5) if targets else (None, 0)
    if effect_type == "destroy":
        targets = find_destroy_targets(hand_cards, max_count, rank_affinity=rank_aff,
                                        strategy=strat)
        return (targets, 1.0) if targets else (None, 0)
    if effect_type == "clone":
        targets = find_clone_targets(hand_cards, strat)
        return (targets, 3.5) if targets else (None, 0)
    if effect_type == "deck_enhance":
        targets = find_deck_enhance_targets(hand_cards, max_count)
        return (targets, 2.0) if targets else (None, 0)
    if effect_type == "seal":
        targets = find_seal_targets(hand_cards, max_count,
                                     seal_type=extra, strategy=strat,
                                     current_best=current_best)
        return (targets, 2.0) if targets else (None, 0)
    if effect_type == "edition":
        targets = find_edition_targets(hand_cards, max_count)
        return (targets, 1.0) if targets else (None, 0)
    if effect_type == "clone_deck":
        targets = find_clone_deck_targets(hand_cards, max_count, current_best)
        return (targets, 1.5) if targets else (None, 0)
    return (None, 0)
