"""The compiled scoring kernel (``kernel/``, Rust), asked the way the bot asks.

This is the only module that talks to ``jackpotts_kernel``. Python still decides
everything that depends on the lineup alone: which jokers are active and whom
Blueprint and Brainstorm copy (``joker_effects.roster``), each joker's numbers
(``joker_effects.parsers``) and each card's numbers (``balatro_bot.cards``). The
kernel does the work that repeats per hand: classifying, ordering and scoring.
"""

from __future__ import annotations

import math
from dataclasses import replace
from typing import TYPE_CHECKING

import jackpotts_kernel as _kernel

from balatro_bot.cards import (
    _SUIT_BIT,
    _modifier,
    card_chip_value,
    card_edition_mult_value,
    card_edition_xmult_value,
    card_facts,
    card_mult_value,
    card_xmult_value,
    joker_key,
    rank_value,
)
from balatro_bot.constants import HAND_INFO, HAND_TYPES
from balatro_bot.domain.models.card import Card
from balatro_bot.domain.models.joker import Joker
from balatro_bot.domain.scoring.estimate import _before_phase_jokers, ox_most_played_hand
from balatro_bot.joker_effects.parsers import _ab_chips, _ab_mult, _ab_xmult, _ability, _current
from balatro_bot.joker_effects.registry import _effect_plan
from balatro_bot.joker_effects.roster import Roster, copy_target, roster

if TYPE_CHECKING:
    from typing import Any

HAND_NAMES: list[str] = [name for name, _, _ in HAND_TYPES]
HAND_IDS: dict[str, int] = {name: i for i, name in enumerate(HAND_NAMES)}

# Card record flags (kernel/core/src/card.rs).
_STONE, _DEBUFFED, _FACE, _RED_SEAL, _STEEL, _WILD, _LUCKY, _GOLD_SEAL = 1, 2, 4, 8, 16, 32, 64, 128
_ENHANCED, _GOLD = 1, 2
_BLINDS = {"The Hook": 1, "The Tooth": 2}


def _pack(card: Any) -> bytes:
    facts = card_facts(card)
    enhancement = _modifier(card).get("enhancement")
    flags = (
        (_STONE if facts.stone else 0) | (_DEBUFFED if facts.debuffed else 0) | (_FACE if facts.face else 0)
        | (_RED_SEAL if facts.red_seal else 0) | (_STEEL if facts.steel else 0)
        | (_WILD if enhancement == "WILD" else 0) | (_LUCKY if enhancement == "LUCKY" else 0)
        | (_GOLD_SEAL if facts.gold_seal else 0)
    )
    flags2 = (_ENHANCED if enhancement and enhancement != "BASE" else 0) | (_GOLD if enhancement == "GOLD" else 0)
    return _kernel.pack_card(
        facts.rank_val if facts.rank else 0, facts.suit_mask, facts.suit_mask_smeared, flags, flags2,
        float(card_chip_value(card)), float(card_mult_value(card)), float(card_xmult_value(card)),
        float(card_edition_mult_value(card)), float(card_edition_xmult_value(card)),
    )


def _with_enhancement(card: Any, enhancement: str | None) -> Any:
    """The card as Midas Mask (Gold) or Vampire (none) would leave it."""
    if isinstance(card, Card):
        return replace(card, modifier=replace(card.modifier, enhancement=enhancement))
    changed = dict(card)
    modifier = dict(_modifier(card))
    if enhancement is None:
        modifier.pop("enhancement", None)
    else:
        modifier["enhancement"] = enhancement
    changed["modifier"] = modifier
    return changed


def _cached(card: Any, name: str, build) -> bytes:
    """A typed Card is frozen, so its record is kept on it; a raw dict's is rebuilt."""
    if type(card) is not Card:
        return build(card)
    record = card.__dict__.get(name)
    if record is None:
        record = card.__dict__[name] = build(card)
    return record


def card_record(card: Any) -> bytes:
    return _cached(card, "_record", _pack)


def gold_record(card: Any) -> bytes:
    return _cached(card, "_record_gold", lambda c: _pack(_with_enhancement(c, "GOLD")))


def plain_record(card: Any) -> bytes:
    return _cached(card, "_record_plain", lambda c: _pack(_with_enhancement(c, None)))


def _records(cards: list, record=card_record) -> bytes:
    return b"".join([record(c) for c in cards])


def _whole(number: float) -> float | int:
    """Chips as the Python kernel reports them: an int when nothing fractional was added."""
    return int(number) if number.is_integer() else number


def _joker_tuple(joker: Any) -> tuple:
    cost = joker.cost if isinstance(joker, Joker) else joker.get("cost") or {}
    counter = _current(joker).get("counter")
    numbers = {
        name: float(value) for name, value in _ability(joker).items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }
    return (
        joker_key(joker), float(cost.get("sell", 0) or 0),
        _ab_chips(joker, fallback=None), _ab_mult(joker, fallback=None), _ab_xmult(joker, fallback=None),
        None if counter is None else float(counter), numbers,
    )


class KernelScorer:
    """One joker lineup in one round situation, scored by the compiled kernel.

    Takes what ``estimate.HandScorer`` takes. *lineup* is ``roster(jokers)`` when
    the caller already has it.
    """

    def __init__(
        self,
        hand_levels: dict[str, dict] | None = None,
        jokers: list | None = None,
        money: int = 0,
        discards_left: int = 0,
        hands_left: int = 1,
        joker_limit: int = 5,
        ancient_suit: str | None = None,
        deck_count: int = 0,
        blind_name: str = "",
        ox_most_played: str | None = None,
        idol_rank: str | None = None,
        idol_suit: str | None = None,
        lineup: Roster | None = None,
    ) -> None:
        jokers = jokers or []
        if lineup is None:
            lineup = roster(jokers)
        self.has_jokers = bool(jokers)
        self.active = lineup.active
        active_keys = lineup.active_keys
        self._variants = bool(_before_phase_jokers(lineup))

        levels = []
        for name, chips, mult in HAND_TYPES:
            level = hand_levels.get(name) if hand_levels else None
            if level is None:
                levels.append((float(chips), float(mult), 0.0, 0.0))
            else:
                levels.append((
                    float(level.get("chips", chips)), float(level.get("mult", mult)),
                    float(level.get("played", 0)), float(level.get("played_this_round", 0)),
                ))

        ox_locked = None
        if jokers and blind_name == "The Ox":
            ox_name = ox_most_played or (ox_most_played_hand(hand_levels) if hand_levels else None)
            ox_locked = HAND_IDS.get(ox_name)

        settings = (
            float(money), float(discards_left), float(hands_left), float(joker_limit), float(deck_count),
            _BLINDS.get(blind_name, 0), ox_locked,
            _SUIT_BIT.get(ancient_suit, 0), rank_value(idol_rank) if idol_rank else 0, _SUIT_BIT.get(idol_suit, 0),
        )
        # Hand search asks about many hands under one lineup and situation: the
        # kernel's lineup is kept on the roster for the next ask.
        built = lineup.derived.setdefault("kernel", {})
        key = (tuple(levels), settings)
        self._kernel = built.get(key)
        if self._kernel is not None:
            return

        at: dict[int, int] = {}
        for i, joker in enumerate(jokers):
            at.setdefault(id(joker), i)

        def runs(joker: Any) -> int | None:
            target = copy_target(jokers, at[id(joker)])
            return None if target is None else at[id(target)]

        self._kernel = built[key] = _kernel.Lineup(
            [_joker_tuple(j) for j in jokers],
            [
                (None if pre_chips is None else float(pre_chips), None if pre_mult is None else float(pre_mult),
                 None if post_xmult is None else float(post_xmult), baseball, runs(joker))
                for joker, _effect, pre_chips, pre_mult, post_xmult, baseball in _effect_plan(lineup)
            ],
            [at[id(joker)] for _, joker in lineup.resolved],
            [at[id(joker)] for _, joker in _before_phase_jokers(lineup)],
            [float(x) for x in lineup.baseball_xmults],
            levels,
            settings,
            (
                "j_pareidolia" in active_keys, "j_smeared" in active_keys, "j_oops" in active_keys,
                "j_four_fingers" in active_keys, "j_shortcut" in active_keys, "j_splash" in active_keys,
                "j_hanging_chad" in active_keys, "j_dna" in active_keys,
            ),
        )

    # -- one play ---------------------------------------------------------------

    def _play(self, hand_name: str, scoring_cards: list, played_cards: list | None, held_cards: list | None) -> tuple:
        """The kernel's arguments for one play. Cards are matched by identity, as the
        Python kernel does: a scoring card that is not among the played ones scores
        after them."""
        played = played_cards or scoring_cards
        where: dict[int, int] = {}
        for i, card in enumerate(played):
            where.setdefault(id(card), i)
        scoring_ids = {id(card) for card in scoring_cards}
        in_scoring = sum(1 << i for i, card in enumerate(played) if id(card) in scoring_ids)
        cards = list(played)
        positions = bytearray()
        for card in scoring_cards:
            position = where.get(id(card))
            if position is None:
                position = where[id(card)] = len(cards)
                cards.append(card)
            positions.append(position)
        gold = _records(cards, gold_record) if self._variants else b""
        plain = _records(cards, plain_record) if self._variants else b""
        return (
            HAND_IDS[hand_name], _records(cards), len(played), bytes(positions), in_scoring,
            _records(held_cards or []), gold, plain,
        )

    def score(
        self, hand_name: str, scoring_cards: list, played_cards: list | None = None,
        held_cards: list | None = None, rng=None,
    ) -> tuple:
        """(chips, mult, total) for one play."""
        chips, mult = self._kernel.score(*self._play(hand_name, scoring_cards, played_cards, held_cards), rng)
        return _whole(chips), mult, math.floor(chips * mult)

    def breakdown(
        self, hand_name: str, scoring_cards: list, played_cards: list | None = None,
        held_cards: list | None = None,
    ) -> dict:
        """The numbers ``score_hand_detailed`` reports besides the per-card details."""
        pre_chips, pre_mult, chips, mult, shares = self._kernel.breakdown(
            *self._play(hand_name, scoring_cards, played_cards, held_cards))
        labels = [
            joker.label if isinstance(joker, Joker) else joker.get("label", key) for key, joker in self.active
        ] if self.has_jokers else []
        return {
            "pre_joker_chips": _whole(pre_chips), "pre_joker_mult": pre_mult,
            "joker_contributions": [
                (label, _whole(dc), dm, ratio) for label, (dc, dm, ratio) in zip(labels, shares)],
            "post_joker_chips": _whole(chips), "post_joker_mult": mult,
            "total": math.floor(chips * mult),
        }

    # -- every play out of a hand -------------------------------------------------

    def _search_args(
        self, hand_cards: list, max_select: int, min_select: int, required_hand: str | None,
        required_card_indices: set[int] | None, excluded_hands: set[str] | None,
        hand_affinity: dict[str, float] | None,
    ) -> tuple:
        gold = _records(hand_cards, gold_record) if self._variants else b""
        plain = _records(hand_cards, plain_record) if self._variants else b""
        return (
            _records(hand_cards), gold, plain, min_select, max_select,
            None if required_hand is None else HAND_IDS.get(required_hand, len(HAND_NAMES)),
            sum(1 << HAND_IDS[name] for name in excluded_hands or () if name in HAND_IDS),
            sum(1 << i for i in required_card_indices or ()),
            [hand_affinity.get(name, 0.0) for name in HAND_NAMES] if hand_affinity else None,
        )

    @staticmethod
    def _found(found: tuple) -> tuple:
        hand_id, played_bits, scoring, chips, mult, total = found
        indices = tuple(i for i in range(played_bits.bit_length()) if played_bits >> i & 1)
        return HAND_NAMES[hand_id], indices, _whole(chips), mult, int(total), tuple(scoring)

    def search(
        self, hand_cards: list, max_select: int = 5, min_select: int = 1, required_hand: str | None = None,
        required_card_indices: set[int] | None = None, excluded_hands: set[str] | None = None,
        hand_affinity: dict[str, float] | None = None,
    ) -> list[tuple]:
        """Every allowed play, best first, as (hand name, card indices, chips, mult,
        total, scoring card indices)."""
        args = self._search_args(hand_cards, max_select, min_select, required_hand,
                                 required_card_indices, excluded_hands, hand_affinity)
        return [self._found(f) for f in self._kernel.search(*args)]

    def best(
        self, hand_cards: list, max_select: int = 5, min_select: int = 1, required_hand: str | None = None,
        required_card_indices: set[int] | None = None, excluded_hands: set[str] | None = None,
        hand_affinity: dict[str, float] | None = None,
    ) -> tuple | None:
        """The best allowed play, or None."""
        args = self._search_args(hand_cards, max_select, min_select, required_hand,
                                 required_card_indices, excluded_hands, hand_affinity)
        found = self._kernel.best(*args)
        return None if found is None else self._found(found)

    def play_order(self, hand_cards: list, indices: list[int]) -> list[int]:
        """The order to play the cards at *indices* in."""
        return list(self._kernel.play_order(_records(hand_cards), bytes(sorted(indices))))
