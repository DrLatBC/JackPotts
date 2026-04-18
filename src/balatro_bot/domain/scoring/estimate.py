"""Hand scoring — compute chips, mult, and total for a played hand.

Moved from hand_evaluator.py during Phase 2 of the logic separation refactor.
"""

from __future__ import annotations

import math
from dataclasses import replace
from typing import TYPE_CHECKING

from balatro_bot.cards import (
    _modifier,
    card_chip_value,
    card_edition_mult_value,
    card_edition_xmult_value,
    card_mult_value,
    card_rank,
    card_suits,
    card_xmult_value,
    is_debuffed,
    is_joker_debuffed,
    is_stone,
    joker_key,
    rank_value,
)
from balatro_bot.constants import HAND_INFO, RANK_CHIPS
from balatro_bot.domain.models.card import Card
from balatro_bot.joker_effects.parsers import _ability

if TYPE_CHECKING:
    from typing import Any


def _apply_before_phase(scoring_cards, played_cards, jokers, pareidolia=False):
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
    from balatro_bot.cards import is_debuffed, _modifier
    from balatro_bot.joker_effects.parsers import _ability, _ab_xmult
    from balatro_bot.constants import FACE_RANKS

    before_jokers = []
    for j in (jokers or []):
        if is_joker_debuffed(j):
            continue
        key = joker_key(j)
        if key in ("j_midas_mask", "j_vampire"):
            before_jokers.append((key, j))

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
            for orig, replacement in remap:
                if c is orig:
                    return replacement
            return c
        effective_played = [_get_remapped(c) for c in played_cards]

    return effective, effective_played, vampire_xmult


def _apply_card_scoring(ctx, scoring_cards, played_cards, jokers, ancient_suit):
    """Score cards in played order with per-card joker effects interleaved.

    In Balatro, each card trigger replays the full per-card sequence:
    base chips -> enhancement mult -> Glass xmult -> edition -> per-card jokers.
    Per-card jokers ("when scored") fire WITHIN each trigger, not after.
    """
    from balatro_bot.joker_effects import retrigger_count
    from balatro_bot.constants import FACE_RANKS, FIBONACCI_RANKS, EVEN_RANKS, ODD_RANKS

    scored_in_play_order = [c for c in (played_cards or scoring_cards)
                            if any(c is s for s in scoring_cards)]
    for c in scoring_cards:
        if not any(c is p for p in scored_in_play_order):
            scored_in_play_order.append(c)

    _per_card = []
    _first_face_found = False

    def _add_per_card_effect(key: str, joker: dict) -> None:
        """Add per-card scoring effects for a joker key, using joker's ability data."""
        ab = _ability(joker)
        if key == "j_greedy_joker":
            _per_card.append(("suit_mult", "D", ab.get("s_mult", 3)))
        elif key == "j_lusty_joker":
            _per_card.append(("suit_mult", "H", ab.get("s_mult", 3)))
        elif key == "j_wrathful_joker":
            _per_card.append(("suit_mult", "S", ab.get("s_mult", 3)))
        elif key == "j_gluttenous_joker":
            _per_card.append(("suit_mult", "C", ab.get("s_mult", 3)))
        elif key == "j_fibonacci":
            _per_card.append(("ranks_mult", FIBONACCI_RANKS, ab.get("extra", 8)))
        elif key == "j_even_steven":
            _per_card.append(("ranks_mult", EVEN_RANKS, ab.get("extra", 4)))
        elif key == "j_odd_todd":
            _per_card.append(("ranks_chips", ODD_RANKS, ab.get("extra", 31)))
        elif key == "j_scholar":
            _per_card.append(("ranks_cm", frozenset({"A"}), ab.get("chips", 20), ab.get("mult", 4)))
        elif key == "j_smiley":
            _per_card.append(("face_mult", ab.get("extra", 5)))
        elif key == "j_scary_face":
            _per_card.append(("face_chips", ab.get("extra", 30)))
        elif key == "j_walkie_talkie":
            _per_card.append(("ranks_cm", frozenset({"T", "4"}), ab.get("chips", 10), ab.get("mult", 4)))
        elif key == "j_photograph":
            _per_card.append(("first_face_xmult", ab.get("extra", 2.0)))
        elif key == "j_triboulet":
            _per_card.append(("ranks_xmult", frozenset({"K", "Q"}), ab.get("extra", 2.0)))
        elif key == "j_ancient":
            _per_card.append(("suit_xmult", ancient_suit, 1.5))
        elif key == "j_arrowhead":
            _per_card.append(("suit_chips", "S", ab.get("extra", 50)))
        elif key == "j_onyx_agate":
            _per_card.append(("suit_mult", "C", ab.get("extra", 7)))
        elif key == "j_bloodstone":
            xm = ab.get("Xmult", 1.5)
            odds = ab.get("odds", 2)
            _per_card.append(("suit_expected_xmult", "H", xm, odds))
        elif key == "j_hiker":
            _per_card.append(("all_chips", ab.get("extra", 5)))

    joker_list = jokers or []
    for i, j in enumerate(joker_list):
        if is_joker_debuffed(j):
            continue
        k = joker_key(j)
        if k == "j_blueprint":
            # Blueprint copies the joker to its right
            if i + 1 < len(joker_list):
                target = joker_list[i + 1]
                if not is_joker_debuffed(target):
                    _add_per_card_effect(joker_key(target), target)
        elif k == "j_brainstorm":
            # Brainstorm copies the leftmost joker
            if joker_list and joker_list[0] is not j:
                target = joker_list[0]
                if not is_joker_debuffed(target):
                    _add_per_card_effect(joker_key(target), target)
        else:
            _add_per_card_effect(k, j)

    for card in scored_in_play_order:
        triggers = retrigger_count(card, ctx)
        _is_first_face_card = False
        if not _first_face_found and not is_debuffed(card):
            rank_check = card_rank(card)
            if ctx.pareidolia or rank_check in FACE_RANKS:
                _is_first_face_card = True
                _first_face_found = True
        for _t in range(triggers):
            ctx.chips += card_chip_value(card)
            ctx.mult += card_mult_value(card)
            xmv = card_xmult_value(card)
            if xmv != 1.0:
                ctx.mult *= xmv
            ed_mult = card_edition_mult_value(card)
            if ed_mult:
                ctx.mult += ed_mult
            ed_xmult = card_edition_xmult_value(card)
            if ed_xmult != 1.0:
                ctx.mult *= ed_xmult
            if not is_debuffed(card):
                rank = card_rank(card)
                suits = card_suits(card, smeared=ctx.smeared) if _per_card else set()
                for eff in _per_card:
                    kind = eff[0]
                    if kind == "ranks_mult" and rank in eff[1]:
                        ctx.mult += eff[2]
                    elif kind == "ranks_chips" and rank in eff[1]:
                        ctx.chips += eff[2]
                    elif kind == "ranks_cm" and rank in eff[1]:
                        ctx.chips += eff[2]
                        ctx.mult += eff[3]
                    elif kind == "face_mult" and (ctx.pareidolia or rank in FACE_RANKS):
                        ctx.mult += eff[1]
                    elif kind == "face_chips" and (ctx.pareidolia or rank in FACE_RANKS):
                        ctx.chips += eff[1]
                    elif kind == "suit_mult" and eff[1] in suits:
                        ctx.mult += eff[2]
                    elif kind == "suit_chips" and eff[1] in suits:
                        ctx.chips += eff[2]
                    elif kind == "suit_xmult" and eff[1] and eff[1] in suits:
                        ctx.mult *= eff[2]
                    elif kind == "suit_expected_xmult" and eff[1] in suits:
                        ctx.mult *= eff[2] ** (1.0 / eff[3])
                    elif kind == "ranks_xmult" and rank in eff[1]:
                        ctx.mult *= eff[2]
                    elif kind == "first_face_xmult":
                        if _is_first_face_card:
                            ctx.mult *= eff[1]
                    elif kind == "all_chips":
                        ctx.chips += eff[1] * _t
            if not is_debuffed(card):
                seal = _modifier(card).get("seal", "")
                if seal == "GOLD":
                    ctx.money += 3

    if ctx.blind_name == "The Tooth":
        ctx.money -= len(ctx.played_cards)

    # Held-card-phase joker detection — resolve Blueprint/Brainstorm targets
    _baron_xm = 0.0
    _baron_count = 0
    _shoot_moon_mult = 0.0
    _shoot_moon_count = 0
    _mime_count = 0
    _raised_fist_count = 0

    def _count_held_phase(hk: str, hj: dict) -> None:
        nonlocal _baron_xm, _baron_count, _shoot_moon_mult, _shoot_moon_count
        nonlocal _mime_count, _raised_fist_count
        if hk == "j_baron":
            from balatro_bot.joker_effects.parsers import _ability as _ab
            _baron_xm = _ab(hj).get("extra", 1.5)
            _baron_count += 1
        elif hk == "j_shoot_the_moon":
            from balatro_bot.joker_effects.parsers import _ability as _ab
            _shoot_moon_mult = _ab(hj).get("extra", 13)
            _shoot_moon_count += 1
        elif hk == "j_mime":
            _mime_count += 1
        elif hk == "j_raised_fist":
            _raised_fist_count += 1

    for i, j in enumerate(joker_list):
        if is_joker_debuffed(j):
            continue
        k = joker_key(j)
        if k == "j_blueprint":
            if i + 1 < len(joker_list):
                target = joker_list[i + 1]
                if not is_joker_debuffed(target):
                    _count_held_phase(joker_key(target), target)
        elif k == "j_brainstorm":
            if joker_list and joker_list[0] is not j:
                target = joker_list[0]
                if not is_joker_debuffed(target):
                    _count_held_phase(joker_key(target), target)
        else:
            _count_held_phase(k, j)

    # Raised Fist: find the lowest-ranked held card (by chip value, matching game logic)
    # The game considers ALL held cards (including debuffed ones) when picking
    # the lowest.  If the chosen card is debuffed, the effect returns 0 mult.
    _raised_fist_card = None
    _raised_fist_add = 0.0
    if _raised_fist_count > 0 and ctx.held_cards:
        min_id = 15
        for c in ctx.held_cards:
            r = card_rank(c)
            if r:
                rv = rank_value(r)
                if rv <= min_id:
                    min_id = rv
                    _raised_fist_card = c
                    _raised_fist_add = 0.0 if is_debuffed(c) else 2 * RANK_CHIPS.get(r, 0)

    _held_triggers = 1 + _mime_count
    for card in ctx.held_cards:
        if not is_debuffed(card):
            for _ in range(_held_triggers):
                if _modifier(card).get("enhancement") == "STEEL":
                    ctx.mult *= 1.5
                if _baron_xm and card_rank(card) == "K":
                    for _ in range(_baron_count):
                        ctx.mult *= _baron_xm
                if _shoot_moon_mult and card_rank(card) == "Q":
                    for _ in range(_shoot_moon_count):
                        ctx.mult += _shoot_moon_mult
                if _raised_fist_card is card:
                    for _ in range(_raised_fist_count):
                        ctx.mult += _raised_fist_add


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
) -> tuple[int, int, int]:
    """Compute (chips, mult, total) for a hand."""
    base_chips, base_mult, _ = HAND_INFO[hand_name]

    if hand_levels and hand_name in hand_levels:
        lvl = hand_levels[hand_name]
        base_chips = lvl.get("chips", base_chips)
        base_mult = lvl.get("mult", base_mult)

    if not jokers:
        total_chips = base_chips
        total_mult  = float(base_mult)
        for c in scoring_cards:
            total_chips += card_chip_value(c)
            total_mult  += card_mult_value(c)
            xmv = card_xmult_value(c)
            if xmv != 1.0:
                total_mult *= xmv
            total_mult += card_edition_mult_value(c)
            exmv = card_edition_xmult_value(c)
            if exmv != 1.0:
                total_mult *= exmv
        for c in (held_cards or []):
            if not is_debuffed(c) and _modifier(c).get("enhancement") == "STEEL":
                total_mult *= 1.5
        total = math.floor(total_chips * total_mult)
        return total_chips, total_mult, total

    from balatro_bot.joker_effects import ScoreContext, apply_joker_effects, retrigger_count

    # The Ox: sets money to $0 when playing the most-played hand type
    # ox_most_played should be pre-computed at blind start (game locks it).
    # Fallback: derive from hand_levels if caller didn't pass it.
    if blind_name == "The Ox":
        _ox_locked = ox_most_played if ox_most_played else (ox_most_played_hand(hand_levels) if hand_levels else None)
        if _ox_locked and hand_name == _ox_locked:
            money = 0

    joker_keys_set = {joker_key(j) for j in jokers if not is_joker_debuffed(j)}
    ctx = ScoreContext(
        chips=base_chips,
        mult=float(base_mult),
        hand_name=hand_name,
        scoring_cards=scoring_cards,
        played_cards=played_cards or scoring_cards,
        held_cards=held_cards or [],
        hand_levels=hand_levels or {},
        jokers=jokers,
        money=money,
        discards_left=discards_left,
        hands_left=hands_left,
        joker_limit=joker_limit,
        deck_count=deck_count,
        deck_cards=deck_cards,
        pareidolia="j_pareidolia" in joker_keys_set,
        smeared="j_smeared" in joker_keys_set,
        ancient_suit=ancient_suit,
        blind_name=blind_name,
        idol_rank=idol_rank,
        idol_suit=idol_suit,
    )

    effective_scoring, effective_played, vampire_xmult = _apply_before_phase(
        scoring_cards, played_cards, jokers, pareidolia=ctx.pareidolia)
    ctx.vampire_xmult = vampire_xmult
    ctx.scoring_cards = effective_scoring
    ctx.played_cards = effective_played or ctx.played_cards

    _apply_card_scoring(ctx, effective_scoring, effective_played, jokers, ancient_suit)

    pre_joker_chips = ctx.chips
    pre_joker_mult = ctx.mult

    apply_joker_effects(ctx)

    total = math.floor(ctx.chips * ctx.mult)

    return ctx.chips, ctx.mult, total


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
    from balatro_bot.joker_effects import ScoreContext, apply_joker_effects_detailed, retrigger_count

    # The Ox: sets money to $0 when playing the most-played hand type
    if blind_name == "The Ox":
        _ox_locked = ox_most_played if ox_most_played else (ox_most_played_hand(hand_levels) if hand_levels else None)
        if _ox_locked and hand_name == _ox_locked:
            money = 0

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

    joker_keys_set = {joker_key(j) for j in jokers if not is_joker_debuffed(j)}
    ctx = ScoreContext(
        chips=base_chips,
        mult=float(base_mult),
        hand_name=hand_name,
        scoring_cards=scoring_cards,
        played_cards=played_cards or scoring_cards,
        held_cards=held_cards or [],
        hand_levels=hand_levels or {},
        jokers=jokers,
        money=money,
        discards_left=discards_left,
        hands_left=hands_left,
        joker_limit=joker_limit,
        deck_count=deck_count,
        deck_cards=deck_cards,
        pareidolia="j_pareidolia" in joker_keys_set,
        smeared="j_smeared" in joker_keys_set,
        ancient_suit=ancient_suit,
        blind_name=blind_name,
        idol_rank=idol_rank,
        idol_suit=idol_suit,
    )

    effective_scoring, effective_played, vampire_xmult = _apply_before_phase(
        scoring_cards, played_cards, jokers, pareidolia=ctx.pareidolia)
    ctx.vampire_xmult = vampire_xmult
    ctx.scoring_cards = effective_scoring
    ctx.played_cards = effective_played or ctx.played_cards

    _apply_card_scoring(ctx, effective_scoring, effective_played, jokers, ancient_suit)

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
