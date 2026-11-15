"""Hand scoring — compute chips, mult, and total for a played hand.
"""

from __future__ import annotations

import math
from dataclasses import replace
from typing import TYPE_CHECKING

from balatro_bot.cards import (
    OP_CHIPS,
    OP_CHIPS_PER_RETRIGGER,
    OP_MULT,
    OP_XMULT,
    _modifier,
    card_chip_value,
    card_edition_mult_value,
    card_edition_xmult_value,
    card_facts,
    card_mult_value,
    card_rank,
    card_xmult_value,
    is_debuffed,
)
from balatro_bot.constants import FACE_RANKS, HAND_INFO, RANK_CHIPS
from balatro_bot.domain.models.card import Card
from balatro_bot.joker_effects import ScoreContext, apply_joker_effects, apply_joker_effects_detailed
from balatro_bot.joker_effects.context import first_played_scoring, retrigger_jokers
from balatro_bot.joker_effects.parsers import _ab_xmult, _ability
from balatro_bot.joker_effects.per_card import per_card_plan
from balatro_bot.joker_effects.roster import Roster, roster

if TYPE_CHECKING:
    from typing import Any


def _before_phase_jokers(lineup: Roster) -> tuple:
    """The lineup's Midas Mask / Vampire entries, in joker order."""
    before = lineup.derived.get("before_phase")
    if before is None:
        before = lineup.derived["before_phase"] = tuple(
            (k, j) for k, j in lineup.active if k in ("j_midas_mask", "j_vampire"))
    return before


def _held_phase_jokers(lineup: Roster) -> tuple:
    """Held-in-hand jokers, Blueprint/Brainstorm copies included.

    Returns (baron_xmult, baron_count, moon_mult, moon_count, mime_count, raised_fist_count).
    """
    held = lineup.derived.get("held_phase")
    if held is None:
        baron_xm = 0.0
        baron_count = 0
        moon_mult = 0.0
        moon_count = 0
        mime_count = 0
        raised_fist_count = 0
        for key, joker in lineup.resolved:
            if key == "j_baron":
                baron_xm = _ability(joker).get("extra", 1.5)
                baron_count += 1
            elif key == "j_shoot_the_moon":
                moon_mult = _ability(joker).get("extra", 13)
                moon_count += 1
            elif key == "j_mime":
                mime_count += 1
            elif key == "j_raised_fist":
                raised_fist_count += 1
        held = lineup.derived["held_phase"] = (
            baron_xm, baron_count, moon_mult, moon_count, mime_count, raised_fist_count)
    return held


def _apply_before_phase(scoring_cards, played_cards, lineup, pareidolia=False):
    """Simulate the game's 'before' phase — jokers that fire before card scoring.

    In the Lua source (state_events.lua:637), context.before iterates jokers
    left-to-right. Both Midas Mask (card.lua:3783) and Vampire (card.lua:3805)
    fire in this phase, mutating cards in-place. Their relative order matters:

      Midas LEFT of Vampire: Midas sets face cards to GOLD, then Vampire
        strips GOLD (and all other enhancements), gaining xmult for them.
      Vampire LEFT of Midas: Vampire strips existing enhancements first,
        then Midas sets face cards to GOLD (they keep GOLD for card scoring).

    This function iterates jokers in list order to match the game's behavior.
    Returns (modified_scoring_cards, modified_played_cards, vampire_xmult).
    Callers must update ctx.scoring_cards with the returned cards so that
    joker_main effects (Flower Pot, Seeing Double, etc.) see the correct
    post-before-phase enhancement state.
    """
    before_jokers = _before_phase_jokers(lineup)

    if not before_jokers:
        return scoring_cards, played_cards, None

    # Work on copies so we don't mutate the caller's lists
    effective = list(scoring_cards)
    remap: list[tuple] = []  # (original, replacement) for played_cards remapping
    vampire_xmult = None

    for key, j in before_jokers:
        if key == "j_midas_mask":
            # Midas: convert all face cards to GOLD enhancement (card.lua:3786-3788)
            for i, card in enumerate(effective):
                rank = card_rank(card)
                if rank and (pareidolia or rank in FACE_RANKS):
                    mod = _modifier(card)
                    if mod.get("enhancement") == "GOLD":
                        continue
                    if isinstance(card, Card):
                        card_copy = replace(card, modifier=replace(card.modifier, enhancement="GOLD"))
                    else:
                        card_copy = dict(card)
                        mod_copy = dict(mod) if isinstance(mod, dict) else {}
                        mod_copy["enhancement"] = "GOLD"
                        card_copy["modifier"] = mod_copy
                    remap.append((effective[i], card_copy))
                    effective[i] = card_copy

        elif key == "j_vampire":
            # Vampire: strip all non-BASE enhancements, gain xmult (card.lua:3805-3833)
            ab = _ability(j)
            extra = ab.get("extra", 0.1)
            current_xmult = _ab_xmult(j, fallback=1.0)
            enhanced_count = 0

            for i, card in enumerate(effective):
                mod = _modifier(card)
                enhancement = mod.get("enhancement", "")
                if enhancement and enhancement != "BASE" and not is_debuffed(card):
                    enhanced_count += 1
                    if isinstance(card, Card):
                        card_copy = replace(card, modifier=replace(card.modifier, enhancement=None))
                    else:
                        card_copy = dict(card)
                        mod_copy = dict(mod)
                        mod_copy.pop("enhancement", None)
                        card_copy["modifier"] = mod_copy
                    remap.append((effective[i], card_copy))
                    effective[i] = card_copy

            vampire_xmult = current_xmult + extra * enhanced_count if enhanced_count > 0 else current_xmult

    # Remap played_cards to reference the modified copies
    effective_played = played_cards
    if remap and played_cards is not None:
        def _get_remapped(c):
            # A card changed twice (Midas, then Vampire) follows both replacements.
            for orig, replacement in remap:
                if c is orig:
                    c = replacement
            return c
        effective_played = [_get_remapped(c) for c in played_cards]

    return effective, effective_played, vampire_xmult


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


class HandScorer:
    """Scores hands under one fixed set of surroundings: jokers, hand levels, money, blind.

    Hand search scores every subset of a hand against the same surroundings, so
    everything that depends only on them is set up once here; ``score`` then
    does the per-hand work. ``score_hand`` is one HandScorer used once.

    *lineup* is ``roster(jokers)`` when the caller already has it.
    """

    def __init__(
        self,
        hand_levels: dict[str, dict] | None = None,
        jokers: list[dict] | None = None,
        money: int = 0,
        discards_left: int = 0,
        hands_left: int = 1,
        joker_limit: int = 5,
        ancient_suit: str | None = None,
        deck_count: int = 0,
        deck_cards: list[dict] | None = None,
        blind_name: str = "",
        ox_most_played: str | None = None,
        idol_rank: str | None = None,
        idol_suit: str | None = None,
        lineup: Roster | None = None,
    ) -> None:
        self._hand_levels = hand_levels
        self._jokers = jokers
        self._money = money
        self._base: dict[str, tuple] = {}  # hand name -> (chips, mult) at its level
        self._facts_of = card_facts
        if not jokers:
            return

        # The Ox: sets money to $0 when playing the most-played hand type.
        # ox_most_played should be pre-computed at blind start (game locks it).
        # Fallback: derive from hand_levels if caller didn't pass it.
        self._ox_locked = None
        if blind_name == "The Ox":
            self._ox_locked = ox_most_played if ox_most_played else (
                ox_most_played_hand(hand_levels) if hand_levels else None)

        if lineup is None:
            lineup = roster(jokers)
        active_keys = lineup.active_keys
        self._ctx = ScoreContext(
            chips=0,
            mult=0.0,
            hand_name="",
            scoring_cards=[],
            played_cards=[],
            held_cards=[],
            hand_levels=hand_levels or {},
            jokers=jokers,
            money=money,
            discards_left=discards_left,
            hands_left=hands_left,
            joker_limit=joker_limit,
            deck_count=deck_count,
            deck_cards=deck_cards,
            pareidolia="j_pareidolia" in active_keys,
            smeared="j_smeared" in active_keys,
            ancient_suit=ancient_suit,
            blind_name=blind_name,
            idol_rank=idol_rank,
            idol_suit=idol_suit,
            oops="j_oops" in active_keys,
            roster=lineup,
        )
        ctx = self._ctx
        self._lineup = lineup
        self._has_before_phase = bool(_before_phase_jokers(lineup))
        self._plan = per_card_plan(
            lineup, ctx.smeared, ctx.pareidolia, idol_rank, idol_suit, ancient_suit)
        self._retriggers = retrigger_jokers(lineup)
        self._every_card_retriggers = (
            (self._retriggers[3] if hands_left == 1 else 0)  # Dusk
            + self._retriggers[4]                            # Seltzer
        )
        self._held_jokers = _held_phase_jokers(lineup)
        self._tooth = blind_name == "The Tooth"

    def use_facts(self, cards: list[dict], facts: list) -> None:
        """Score *cards* with these already-built CardFacts.

        For raw card dicts, whose facts are otherwise rebuilt on every lookup:
        the caller vouches that the dicts stay unchanged while it scores.
        """
        known = {id(c): f for c, f in zip(cards, facts)}

        def facts_of(card):
            return known.get(id(card)) or card_facts(card)

        self._facts_of = facts_of
        # Keeps the ids above from being reused by other objects.
        self._facts_cards = list(cards)

    def _hand_base(self, hand_name: str) -> tuple:
        base = self._base.get(hand_name)
        if base is None:
            base_chips, base_mult, _ = HAND_INFO[hand_name]
            hand_levels = self._hand_levels
            if hand_levels and hand_name in hand_levels:
                lvl = hand_levels[hand_name]
                base_chips = lvl.get("chips", base_chips)
                base_mult = lvl.get("mult", base_mult)
            base = self._base[hand_name] = (base_chips, float(base_mult))
        return base

    def score(
        self,
        hand_name: str,
        scoring_cards: list[dict],
        played_cards: list[dict] | None = None,
        held_cards: list[dict] | None = None,
        rng=None,
    ) -> tuple[int, int, int]:
        """Compute (chips, mult, total) for a hand."""
        if not self._jokers:
            facts_of = self._facts_of
            total_chips, total_mult = self._hand_base(hand_name)
            for c in scoring_cards:
                for code, value in facts_of(c).base_ops:
                    if code == OP_CHIPS:
                        total_chips += value
                    elif code == OP_MULT:
                        total_mult += value
                    else:
                        total_mult *= value
            for c in (held_cards or []):
                facts = facts_of(c)
                if facts.steel and not facts.debuffed:
                    total_mult *= 1.5
            return total_chips, total_mult, math.floor(total_chips * total_mult)

        ctx = self.score_cards(hand_name, scoring_cards, played_cards, held_cards, rng)
        apply_joker_effects(ctx)
        return ctx.chips, ctx.mult, math.floor(ctx.chips * ctx.mult)

    def score_cards(
        self,
        hand_name: str,
        scoring_cards: list[dict],
        played_cards: list[dict] | None = None,
        held_cards: list[dict] | None = None,
        rng=None,
    ) -> ScoreContext:
        """Score everything before the joker phase: the before phase (Midas Mask,
        Vampire), the played cards, then the cards held in hand.

        Returns this scorer's ScoreContext, ready for the joker effects. The same
        object is reused by the next call.

        In Balatro, each card trigger replays the full per-card sequence:
        base chips -> enhancement mult -> Glass xmult -> edition -> per-card jokers.
        Per-card jokers ("when scored") fire WITHIN each trigger, not after.
        Each card's sequence comes precompiled from PerCardPlan.ops_for.
        """
        ctx = self._ctx
        chips, mult = self._hand_base(hand_name)
        ctx.hand_name = hand_name
        ctx.money = 0 if hand_name == self._ox_locked else self._money
        ctx.held_cards = held_cards = held_cards or []
        ctx.rng = rng
        if self._has_before_phase:
            effective_scoring, effective_played, ctx.vampire_xmult = _apply_before_phase(
                scoring_cards, played_cards, self._lineup, pareidolia=ctx.pareidolia)
            ctx.scoring_cards = effective_scoring
            ctx.played_cards = effective_played or played_cards or scoring_cards
            scoring_cards, played_cards = effective_scoring, effective_played
        else:
            ctx.scoring_cards = scoring_cards
            ctx.played_cards = played_cards or scoring_cards

        scoring_ids = {id(c) for c in scoring_cards}
        scored_in_play_order = [c for c in (played_cards or scoring_cards) if id(c) in scoring_ids]
        if len(scored_in_play_order) != len(scoring_cards):
            seen = {id(c) for c in scored_in_play_order}
            for c in scoring_cards:
                if id(c) not in seen:
                    seen.add(id(c))
                    scored_in_play_order.append(c)

        facts_of = self._facts_of
        plan = self._plan
        has_effects = bool(plan.effects)
        hack, sock, chad, _, _ = self._retriggers
        every_card_retriggers = self._every_card_retriggers
        chad_card = first_played_scoring(ctx) if chad else None
        pareidolia = ctx.pareidolia

        first_face_found = False
        for card in scored_in_play_order:
            facts = facts_of(card)
            triggers = 1
            is_first_face = False
            if not facts.debuffed:
                triggers += every_card_retriggers
                if facts.hack_rank:
                    triggers += hack
                if facts.face or pareidolia:
                    triggers += sock
                if chad and card is chad_card:
                    triggers += 2 * chad
                if facts.red_seal:
                    triggers += 1
                if not first_face_found and (pareidolia or facts.face):
                    is_first_face = True
                    first_face_found = True
                if facts.gold_seal:
                    ctx.money += 3 * triggers
            ops = plan.ops_for(facts, is_first_face) if has_effects else facts.base_ops
            for trigger in range(triggers):
                for code, value in ops:
                    if code == OP_CHIPS:
                        chips += value
                    elif code == OP_MULT:
                        mult += value
                    elif code == OP_XMULT:
                        mult *= value
                    elif code == OP_CHIPS_PER_RETRIGGER:
                        chips += value * trigger
                    else:  # OP_CHANCE_XMULT
                        xmult, odds = value
                        prob_factor = 2.0 if ctx.oops else 1.0
                        if rng is not None:
                            if rng.random() < prob_factor / odds:
                                mult *= xmult
                        else:
                            mult *= xmult ** (prob_factor / odds)

        if self._tooth:
            ctx.money -= len(ctx.played_cards)

        # Held-in-hand phase: Steel cards, then Baron / Shoot the Moon / Raised Fist per held card.
        if held_cards:
            baron_xm, baron_count, moon_mult, moon_count, mime_count, raised_fist_count = self._held_jokers

            # Raised Fist: the lowest-ranked held card (by rank, matching game logic).
            # The game considers ALL held cards (including debuffed ones) when picking
            # the lowest.  If the chosen card is debuffed, the effect returns 0 mult.
            raised_fist_card = None
            raised_fist_add = 0.0
            if raised_fist_count > 0:
                min_id = 15
                for c in held_cards:
                    facts = facts_of(c)
                    if facts.rank and facts.rank_val <= min_id:
                        min_id = facts.rank_val
                        raised_fist_card = c
                        raised_fist_add = 0.0 if facts.debuffed else 2 * RANK_CHIPS.get(facts.rank, 0)

            held_triggers = 1 + mime_count
            for card in held_cards:
                facts = facts_of(card)
                if facts.debuffed:
                    continue
                rank = facts.rank
                for _ in range(held_triggers):
                    if facts.steel:
                        mult *= 1.5
                    if baron_xm and rank == "K":
                        for _ in range(baron_count):
                            mult *= baron_xm
                    if moon_mult and rank == "Q":
                        for _ in range(moon_count):
                            mult += moon_mult
                    if raised_fist_card is card:
                        for _ in range(raised_fist_count):
                            mult += raised_fist_add

        ctx.chips = chips
        ctx.mult = mult
        return ctx


def score_hand(
    hand_name: str,
    scoring_cards: list[dict],
    hand_levels: dict[str, dict] | None = None,
    jokers: list[dict] | None = None,
    played_cards: list[dict] | None = None,
    held_cards: list[dict] | None = None,
    money: int = 0,
    discards_left: int = 0,
    hands_left: int = 1,
    joker_limit: int = 5,
    ancient_suit: str | None = None,
    deck_count: int = 0,
    deck_cards: list[dict] | None = None,
    blind_name: str = "",
    ox_most_played: str | None = None,
    idol_rank: str | None = None,
    idol_suit: str | None = None,
    rng=None,
    lineup: Roster | None = None,
) -> tuple[int, int, int]:
    """Compute (chips, mult, total) for a hand.

    *lineup* is ``roster(jokers)`` when the caller already has it.
    """
    return HandScorer(
        hand_levels, jokers, money=money, discards_left=discards_left, hands_left=hands_left,
        joker_limit=joker_limit, ancient_suit=ancient_suit,
        deck_count=deck_count, deck_cards=deck_cards, blind_name=blind_name,
        ox_most_played=ox_most_played, idol_rank=idol_rank, idol_suit=idol_suit,
        lineup=lineup,
    ).score(hand_name, scoring_cards, played_cards, held_cards, rng)


def score_hand_detailed(
    hand_name: str,
    scoring_cards: list[dict],
    hand_levels: dict[str, dict] | None = None,
    jokers: list[dict] | None = None,
    played_cards: list[dict] | None = None,
    held_cards: list[dict] | None = None,
    money: int = 0,
    discards_left: int = 0,
    hands_left: int = 1,
    joker_limit: int = 5,
    ancient_suit: str | None = None,
    deck_count: int = 0,
    deck_cards: list[dict] | None = None,
    blind_name: str = "",
    ox_most_played: str | None = None,
    idol_rank: str | None = None,
    idol_suit: str | None = None,
) -> dict:
    """Like score_hand but returns a full breakdown dict for logging."""
    base_chips, base_mult, _ = HAND_INFO[hand_name]
    if hand_levels and hand_name in hand_levels:
        lvl = hand_levels[hand_name]
        base_chips = lvl.get("chips", base_chips)
        base_mult = lvl.get("mult", base_mult)

    card_details = []
    for c in scoring_cards:
        if isinstance(c, Card):
            label = c.label or "?"
            chips = card_chip_value(c)
            mult = card_mult_value(c)
            xmult = card_xmult_value(c)
            mod = c.modifier
            if mod.edition or mod.enhancement:
                label += f"[{mod.edition or ''}/{mod.enhancement or ''}]"
        else:
            label = c.get("label", "?")
            chips = card_chip_value(c)
            mult = card_mult_value(c)
            xmult = card_xmult_value(c)
            mod = c.get("modifier", {})
            if isinstance(mod, dict) and (mod.get("edition") or mod.get("enhancement")):
                label += f"[{mod.get('edition','')}/{mod.get('enhancement','')}]"
        card_details.append((label, chips, mult, xmult))

    if not jokers:
        total_chips = base_chips
        total_mult = float(base_mult)
        for c, (_, chips, mult, xmult) in zip(scoring_cards, card_details):
            total_chips += chips
            total_mult += mult
            if xmult != 1.0:
                total_mult *= xmult
            total_mult += card_edition_mult_value(c)
            exmv = card_edition_xmult_value(c)
            if exmv != 1.0:
                total_mult *= exmv
        for c in (held_cards or []):
            if not is_debuffed(c) and _modifier(c).get("enhancement") == "STEEL":
                total_mult *= 1.5
        return {
            "hand_name": hand_name,
            "base_chips": base_chips, "base_mult": base_mult,
            "card_details": card_details,
            "pre_joker_chips": total_chips, "pre_joker_mult": total_mult,
            "joker_contributions": [],
            "post_joker_chips": total_chips, "post_joker_mult": total_mult,
            "total": math.floor(total_chips * total_mult),
        }

    ctx = HandScorer(
        hand_levels, jokers, money=money, discards_left=discards_left, hands_left=hands_left,
        joker_limit=joker_limit, ancient_suit=ancient_suit,
        deck_count=deck_count, deck_cards=deck_cards, blind_name=blind_name,
        ox_most_played=ox_most_played, idol_rank=idol_rank, idol_suit=idol_suit,
    ).score_cards(hand_name, scoring_cards, played_cards, held_cards)

    pre_joker_chips = ctx.chips
    pre_joker_mult = ctx.mult

    joker_contributions = apply_joker_effects_detailed(ctx)

    total = math.floor(ctx.chips * ctx.mult)
    return {
        "hand_name": hand_name,
        "base_chips": base_chips, "base_mult": base_mult,
        "card_details": card_details,
        "pre_joker_chips": pre_joker_chips, "pre_joker_mult": pre_joker_mult,
        "joker_contributions": joker_contributions,
        "post_joker_chips": ctx.chips, "post_joker_mult": ctx.mult,
        "total": total,
    }
