"""Left-to-right play order for a chosen hand."""

from __future__ import annotations

from typing import TYPE_CHECKING

from balatro_bot.cards import card_rank, card_suit, card_xmult_value, card_chip_value, card_mult_value, joker_key, is_debuffed
from balatro_bot.constants import FACE_RANKS
from balatro_bot.joker_effects.per_card import (
    PerCardCtx,
    build_per_card_effects,
    card_contribution,
)

if TYPE_CHECKING:
    from balatro_bot.strategy import Strategy


def sort_play_order(
    indices: list[int],
    hand_cards: list[dict],
    jokers: list[dict],
    strategy: "Strategy | None" = None,
    *,
    idol_rank: str | None = None,
    idol_suit: str | None = None,
    ancient_suit: str | None = None,
) -> list[int]:
    """Sort card play indices so additive effects fire before multiplicative.

    In Balatro, played cards score left-to-right. Cards that gain ×mult — whether
    from the card itself (Glass, Polychrome) or from a per-card joker (Triboulet
    on K/Q, Idol on target rank+suit, Ancient on matching suit, Bloodstone on
    hearts, etc.) — should be rightmost so all +chips/+mult accumulate first.

    With Hanging Chad, the first card gets +2 retriggers (3 total). The card
    that benefits most from compounded retriggers goes first instead.

    Photograph fires ×2 mult once on the first face card played; we put the
    highest-value face first so Photograph compounds with its other xmult
    sources (Triboulet, Idol) on the same trigger.

    All per-card joker scoring math is sourced from
    ``joker_effects.per_card.build_per_card_effects`` — no hardcoded joker
    names in this file. Registering a new per-card joker there makes it
    visible to the sequencer automatically.
    """
    if len(indices) <= 1:
        return indices

    joker_keys = {joker_key(j) for j in jokers}
    pc_ctx = PerCardCtx(
        smeared="j_smeared" in joker_keys,
        pareidolia="j_pareidolia" in joker_keys,
        idol_rank=idol_rank,
        idol_suit=idol_suit,
        ancient_suit=ancient_suit,
    )
    effects = build_per_card_effects(jokers, pc_ctx)
    has_hanging_chad = "j_hanging_chad" in joker_keys
    has_photograph = any(e[0] == "first_face_xmult" for e in effects)
    has_dna = "j_dna" in joker_keys

    def _is_face(idx: int) -> bool:
        return pc_ctx.pareidolia or card_rank(hand_cards[idx]) in FACE_RANKS

    def _card_xmult(idx: int, *, is_first_face: bool = False) -> float:
        c = hand_cards[idx]
        if is_debuffed(c):
            return 1.0
        _, _, jx = card_contribution(c, effects, pc_ctx, is_first_face=is_first_face)
        return card_xmult_value(c) * jx

    def _card_add(idx: int) -> float:
        c = hand_cards[idx]
        if is_debuffed(c):
            return 0.0
        dc, dm, _ = card_contribution(c, effects, pc_ctx, is_first_face=False)
        return card_chip_value(c) + card_mult_value(c) + dc + dm

    def _sort_rest(rest: list[int]) -> list[int]:
        """Additive-only cards left, any-xmult cards right (stable within groups)."""
        rest.sort(key=lambda i: (0 if _card_xmult(i) <= 1.0 else 1, i))
        return rest

    if has_hanging_chad:
        def _first_position_score(idx: int) -> float:
            c = hand_cards[idx]
            if is_debuffed(c):
                return 0.0
            xm = _card_xmult(idx, is_first_face=_is_face(idx))
            add = _card_add(idx)
            # 3 triggers in first position: xm^3 for multiplicative,
            # 3*add vs 1*add = 2*add extra for additive
            return xm ** 3 + 2 * add

        best_first = max(indices, key=_first_position_score)
        rest = [i for i in indices if i != best_first]
        return [best_first] + _sort_rest(rest)

    # Photograph without Hanging Chad: put best face card first so its x2
    # compounds with other per-card xmult sources on the same trigger.
    if has_photograph:
        face_indices = [i for i in indices if not is_debuffed(hand_cards[i]) and _is_face(i)]
        if face_indices:
            best_face = max(face_indices, key=lambda i: _card_xmult(i, is_first_face=True))
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
            if score == 0.0:
                score = card_chip_value(c) / 100.0
            return score

        best_dna = max(indices, key=_dna_value)
        rest = [i for i in indices if i != best_dna]
        return [best_dna] + _sort_rest(rest)

    # Default: additive left, any-xmult right
    return sorted(indices, key=lambda i: (
        0 if _card_xmult(i) <= 1.0 else 1,
        i,
    ))
