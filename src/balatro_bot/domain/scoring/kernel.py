"""The compiled scoring kernel (``kernel/``, Rust), asked the way the bot asks.

This is the only module that talks to ``jackpotts_kernel``, and the bot's only
scorer. Python still decides
everything that depends on the lineup alone: which jokers are active and whom
Blueprint and Brainstorm copy (``joker_effects.roster``), each joker's numbers
(``joker_effects.parsers``) and each card's numbers (``balatro_bot.cards``). The
kernel does the work that repeats per hand: classifying, ordering and scoring.
"""

from __future__ import annotations

import math
from dataclasses import replace
from typing import TYPE_CHECKING

try:
    import jackpotts_kernel as _kernel
except ImportError as err:
    raise ImportError(
        "The compiled scoring kernel (jackpotts_kernel) is not built into this environment. "
        "Build it as docs/installation.md describes (maturin develop --release in kernel/py)."
    ) from err

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
from balatro_bot.constants import HAND_TYPES
from balatro_bot.domain.models.card import Card
from balatro_bot.domain.models.joker import Joker
from balatro_bot.joker_effects.parsers import _ab_chips, _ab_mult, _ab_xmult, _ability, _current
from balatro_bot.joker_effects.roster import Roster, copy_target, roster

if TYPE_CHECKING:
    from typing import Any

HAND_NAMES: list[str] = [name for name, _, _ in HAND_TYPES]
HAND_IDS: dict[str, int] = {name: i for i, name in enumerate(HAND_NAMES)}

# Card record flags (kernel/core/src/card.rs).
_STONE, _DEBUFFED, _FACE, _RED_SEAL, _STEEL, _WILD, _LUCKY, _GOLD_SEAL = 1, 2, 4, 8, 16, 32, 64, 128
_ENHANCED, _GOLD = 1, 2
_BLINDS = {"The Hook": 1, "The Tooth": 2}


_JOKER_PHASES: dict[str, str] = _kernel.joker_phases()


def joker_phases() -> dict[str, str]:
    """Every joker that scores in the joker phase: "chips", "mult", "xmult", or
    "other" for jokers with their own logic."""
    return _JOKER_PHASES


def ox_most_played_hand(hand_levels: dict) -> str | None:
    """Return the hand type with the highest played count.

    The Ox locks this at blind start — call once and cache the result.
    Returns None if no hand has been played yet.
    """
    best_hand = None
    best_count = 0
    for ht, info in hand_levels.items():
        if hasattr(info, "get"):
            played = info.get("played", 0)
            if played > best_count:
                best_count = played
                best_hand = ht
    return best_hand


def _before_phase_jokers(lineup: Roster) -> tuple:
    """The lineup's Midas Mask / Vampire entries, in joker order."""
    before = lineup.derived.get("before_phase")
    if before is None:
        before = lineup.derived["before_phase"] = tuple(
            (k, j) for k, j in lineup.active if k in ("j_midas_mask", "j_vampire"))
    return before


def _is_uncommon(joker: Any) -> bool:
    """Check if a joker is Uncommon rarity (API sends int or string)."""
    if isinstance(joker, Joker):
        return joker.value.rarity in (2, "Uncommon")
    return joker.get("value", {}).get("rarity") in (2, "Uncommon")


def _edition_plan(lineup: Roster) -> tuple:
    """Per active joker: (joker, edition +chips, edition +mult, edition xmult, baseball).

    Edition numbers are None when the edition adds nothing: Foil gives chips and
    Holo gives mult before the joker's effect, Polychrome multiplies after it
    (Negative only adds a slot). baseball marks Uncommon jokers when a Baseball
    Card is in effect.
    """
    plan = lineup.derived.get("edition_plan")
    if plan is None:
        baseball_xm = lineup.baseball_xmults
        steps = []
        for key, joker in lineup.active:
            mod = _modifier(joker)
            edition = mod.get("edition", "")
            pre_chips = mod.get("edition_chips", 50) if edition == "FOIL" else None
            pre_mult = mod.get("edition_mult", 10) if edition in ("HOLO", "HOLOGRAPHIC") else None
            post_xmult = None
            if edition == "POLYCHROME":
                post_xmult = mod.get("edition_x_mult", 1.5)
            elif mod.get("edition_x_mult") and edition not in ("FOIL", "HOLO", "HOLOGRAPHIC", "NEGATIVE"):
                post_xmult = mod["edition_x_mult"]
            baseball = bool(baseball_xm) and key != "j_baseball" and _is_uncommon(joker)
            steps.append((joker, pre_chips, pre_mult, post_xmult, baseball))
        plan = lineup.derived["edition_plan"] = tuple(steps)
    return plan


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


def card_record(card: Any) -> bytes:
    """The card's record. Equal typed cards share one CardFacts, and the record is
    kept there; a raw dict's is rebuilt on every call."""
    facts = card_facts(card)
    record = facts.record
    if record is None:
        record = _pack(card)
        if facts.cached:
            facts.record = record
    return record


def gold_record(card: Any) -> bytes:
    """The card's record as Midas Mask would leave it."""
    facts = card_facts(card)
    record = facts.record_gold
    if record is None:
        record = _pack(_with_enhancement(card, "GOLD"))
        if facts.cached:
            facts.record_gold = record
    return record


def plain_record(card: Any) -> bytes:
    """The card's record as Vampire would leave it."""
    facts = card_facts(card)
    record = facts.record_plain
    if record is None:
        record = _pack(_with_enhancement(card, None))
        if facts.cached:
            facts.record_plain = record
    return record


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


def prepare_play(
    hand_name: str, scoring_cards: list, played_cards: list | None, held_cards: list | None,
    variants: bool = True,
) -> tuple:
    """One play as the kernel takes it. Cards are matched by identity, as the Python
    kernel does: a scoring card that is not among the played ones scores after them.

    *variants* adds each card as Midas Mask and Vampire would leave it, which only
    a lineup holding one of them reads. A prepared play can be scored under any lineup.
    """
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
    return (
        HAND_IDS[hand_name], _records(cards), len(played), bytes(positions), in_scoring,
        _records(held_cards or []),
        _records(cards, gold_record) if variants else b"",
        _records(cards, plain_record) if variants else b"",
    )


class KernelScorer:
    """One joker lineup in one round situation, scored by the compiled kernel.

    *lineup* is ``roster(jokers)`` when the caller already has it.
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
                for joker, pre_chips, pre_mult, post_xmult, baseball in _edition_plan(lineup)
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
        return prepare_play(hand_name, scoring_cards, played_cards, held_cards, self._variants)

    def score_many(self, plays: list[tuple], rngs: list | None = None) -> list[tuple[float, float]]:
        """(chips, mult) for each play from ``prepare_play``, one generator per play if given."""
        return self._kernel.score_many(plays, rngs)

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
        """The numbers ``score_hand_detailed`` reports besides the per-card details.

        ``card_triggers`` is how many times each of *scoring_cards* triggered.
        """
        pre_chips, pre_mult, chips, mult, shares, triggers = self._kernel.breakdown(
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
            "card_triggers": list(triggers),
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

    def best_totals(self, hands: list[list]) -> list[int]:
        """The best play's total for each hand (0 where a hand allows no play), in one call."""
        if self._variants:
            packed = [(_records(h), _records(h, gold_record), _records(h, plain_record)) for h in hands]
        else:
            packed = [(_records(h), b"", b"") for h in hands]
        return [0 if total is None else int(total) for total in self._kernel.best_totals(packed)]

    def play_order(self, hand_cards: list, indices: list[int]) -> list[int]:
        """The order to play the cards at *indices* in."""
        return list(self._kernel.play_order(_records(hand_cards), bytes(sorted(indices))))
