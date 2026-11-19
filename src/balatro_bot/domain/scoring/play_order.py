"""Left-to-right play order for a chosen hand."""

from __future__ import annotations

from typing import TYPE_CHECKING

from balatro_bot.cards import (
    card_chip_value, card_edition_mult_value, card_edition_xmult_value, card_mult_value,
    card_rank, card_suit, card_xmult_value, is_debuffed,
)
from balatro_bot.constants import FACE_RANKS
from balatro_bot.joker_effects.roster import Roster, roster
from balatro_bot.joker_effects.per_card import (
    card_contribution,
    per_card_plan,
)

if TYPE_CHECKING:
    from balatro_bot.strategy import Strategy


class PlayOrderer:
    """Orders plays out of one hand under one joker lineup.

    Hand search orders every subset of the same hand, so each card's ordering
    values are worked out once here and reused across subsets.

    In Balatro, played cards score left-to-right by hand position. Chips are
    summed, so only +mult against ×mult depends on order: a card that
    multiplies (Glass, Polychrome, or a per-card ×mult joker such as Bloodstone
    or Triboulet) should score after every card that adds mult. Cards keep
    hand order unless a multiplying card would score before an adding one,
    so the bot rearranges the hand only when that changes the score.

    With Hanging Chad, the first card gets +2 retriggers (3 total). The card
    that benefits most from compounded retriggers goes first instead.

    Photograph fires ×2 mult once on the first face card played; the
    highest-value face is placed before every other face so Photograph
    compounds with its other xmult sources (Triboulet, Idol).

    All per-card joker scoring math is sourced from ``joker_effects.per_card``
    — no hardcoded joker names in this file. Registering a new per-card joker
    there makes it visible to the sequencer automatically.
    """

    def __init__(
        self,
        hand_cards: list[dict],
        jokers: list[dict],
        strategy: "Strategy | None" = None,
        *,
        idol_rank: str | None = None,
        idol_suit: str | None = None,
        ancient_suit: str | None = None,
        lineup: Roster | None = None,
    ) -> None:
        if lineup is None:
            lineup = roster(jokers)
        joker_keys = lineup.active_keys
        plan = per_card_plan(
            lineup,
            smeared="j_smeared" in joker_keys,
            pareidolia="j_pareidolia" in joker_keys,
            idol_rank=idol_rank,
            idol_suit=idol_suit,
            ancient_suit=ancient_suit,
        )
        self._hand_cards = hand_cards
        self._strategy = strategy
        self._pc_ctx = plan.ctx
        self._effects = plan.effects
        self._has_hanging_chad = "j_hanging_chad" in joker_keys
        self._has_photograph = any(e[0] == "first_face_xmult" for e in self._effects)
        self._has_dna = "j_dna" in joker_keys
        self._xmult: dict[int, float] = {}
        self._xmult_first_face: dict[int, float] = {}
        self._adds_mult: dict[int, bool] = {}

    def _is_face(self, idx: int) -> bool:
        return self._pc_ctx.pareidolia or card_rank(self._hand_cards[idx]) in FACE_RANKS

    def _card_xmult(self, idx: int, is_first_face: bool = False) -> float:
        memo = self._xmult_first_face if is_first_face else self._xmult
        value = memo.get(idx)
        if value is None:
            c = self._hand_cards[idx]
            if is_debuffed(c):
                value = 1.0
            else:
                _, _, jx = card_contribution(c, self._effects, self._pc_ctx, is_first_face=is_first_face)
                value = card_xmult_value(c) * card_edition_xmult_value(c) * jx
            memo[idx] = value
        return value

    def _card_add(self, idx: int) -> float:
        c = self._hand_cards[idx]
        if is_debuffed(c):
            return 0.0
        dc, dm, _ = card_contribution(c, self._effects, self._pc_ctx, is_first_face=False)
        return card_chip_value(c) + card_mult_value(c) + dc + dm

    def _card_adds_mult(self, idx: int) -> bool:
        value = self._adds_mult.get(idx)
        if value is None:
            c = self._hand_cards[idx]
            if is_debuffed(c):
                value = False
            else:
                _, dm, _ = card_contribution(c, self._effects, self._pc_ctx, is_first_face=False)
                value = card_mult_value(c) + card_edition_mult_value(c) + dm > 0
            self._adds_mult[idx] = value
        return value

    def _additive_left(self, indices: list[int], photographed: int | None = None) -> list[int]:
        """Hand order, unless a multiplying card would score before one that adds mult;
        then adding cards go left of multiplying ones (hand order within each group).

        ``photographed`` is the face card Photograph's x2 lands on.
        """
        def multiplies(j: int) -> bool:
            return self._card_xmult(j, is_first_face=j == photographed) > 1.0

        ordered = sorted(indices)
        multiplied = False
        for i in ordered:
            if multiplied and self._card_adds_mult(i):
                return sorted(ordered, key=lambda j: (multiplies(j), j))
            if multiplies(i):
                multiplied = True
        return ordered

    def _first_position_score(self, idx: int) -> float:
        if is_debuffed(self._hand_cards[idx]):
            return 0.0
        xm = self._card_xmult(idx, is_first_face=self._is_face(idx))
        add = self._card_add(idx)
        # 3 triggers in first position: xm^3 for multiplicative,
        # 3*add vs 1*add = 2*add extra for additive
        return xm ** 3 + 2 * add

    def _dna_value(self, idx: int) -> float:
        c = self._hand_cards[idx]
        if is_debuffed(c):
            return -1.0
        rank = card_rank(c)
        suit = card_suit(c)
        score = 0.0
        strategy = self._strategy
        if strategy:
            score += strategy.rank_affinity(rank) if rank else 0.0
            score += strategy.suit_affinity(suit) if suit else 0.0
        if score == 0.0:
            score = card_chip_value(c) / 100.0
        return score

    def order(self, indices: list[int]) -> list[int]:
        """Sort card play indices so additive effects fire before multiplicative."""
        if len(indices) <= 1:
            return indices

        if self._has_hanging_chad:
            best_first = max(indices, key=self._first_position_score)
            return [best_first] + self._additive_left([i for i in indices if i != best_first])

        # Photograph without Hanging Chad: the best face card scores before every
        # other face, so its x2 compounds with other per-card xmult sources.
        if self._has_photograph:
            hand_cards = self._hand_cards
            face_indices = [i for i in indices if not is_debuffed(hand_cards[i]) and self._is_face(i)]
            if face_indices:
                best_face = max(face_indices, key=lambda i: self._card_xmult(i, is_first_face=True))
                order = self._additive_left(indices, photographed=best_face)
                first_face = next(k for k, i in enumerate(order) if i in face_indices)
                if order[first_face] != best_face:
                    order.remove(best_face)
                    order.insert(first_face, best_face)
                return order

        # DNA: first card played gets copied to deck — pick the most strategic card
        if self._has_dna:
            best_dna = max(indices, key=self._dna_value)
            return [best_dna] + self._additive_left([i for i in indices if i != best_dna])

        return self._additive_left(indices)


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
    """Order one play's card indices; see PlayOrderer."""
    if len(indices) <= 1:
        return indices
    return PlayOrderer(
        hand_cards, jokers, strategy,
        idol_rank=idol_rank, idol_suit=idol_suit, ancient_suit=ancient_suit,
    ).order(indices)


def hand_order_for_play(order: list[int], hand_size: int) -> list[int]:
    """The hand rearrangement that plays ``order`` left to right: the played cards swap
    among their own slots and every other card stays put. ``result[k]`` is the current
    index of the card for position k; the play is then the slots ``sorted(order)``."""
    result = list(range(hand_size))
    for slot, idx in zip(sorted(order), order):
        result[slot] = idx
    return result


def play_plan(order: list[int], hand_size: int) -> tuple[list[int] | None, list[int]]:
    """How to play ``order`` left to right: the hand rearrangement to make first (None
    when the cards already sit in that order) and the hand positions to play after it."""
    played = sorted(order)
    if list(order) == played:
        return None, played
    return hand_order_for_play(order, hand_size), played
