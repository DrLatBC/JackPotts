"""Hand scoring — compute chips, mult, and total for a played hand.

Moved from hand_evaluator.py during Phase 2 of the logic separation refactor.
"""

from __future__ import annotations

import math
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
    rank_value,
)
from balatro_bot.constants import HAND_INFO

if TYPE_CHECKING:
    from typing import Any


def _apply_before_phase(scoring_cards, played_cards, jokers):
    """Simulate the game's 'before' phase — jokers that fire before card scoring.

    Vampire: strips enhancement from enhanced scoring cards, gains xmult.
    Returns (modified_scoring_cards, modified_played_cards, vampire_xmult).
    The played_cards list is updated so that stripped scoring cards replace
    their originals (needed for id()-based matching in _apply_card_scoring).
    """
    vampire = None
    for j in (jokers or []):
        if j.get("key") == "j_vampire" and not is_joker_debuffed(j):
            vampire = j
            break
    if not vampire:
        return scoring_cards, played_cards, None

    from balatro_bot.cards import is_debuffed, _modifier
    from balatro_bot.joker_effects.parsers import _ability, _ab_xmult

    ab = _ability(vampire)
    extra = ab.get("extra", 0.1)
    current_xmult = _ab_xmult(vampire, fallback=1.0)

    enhanced_count = 0
    orig_to_stripped = {}
    stripped_scoring = []
    for card in scoring_cards:
        mod = _modifier(card)
        enhancement = mod.get("enhancement", "")
        if enhancement and enhancement != "BASE" and not is_debuffed(card):
            enhanced_count += 1
            card_copy = dict(card)
            mod_copy = dict(mod)
            mod_copy.pop("enhancement", None)
            card_copy["modifier"] = mod_copy
            orig_to_stripped[id(card)] = card_copy
            stripped_scoring.append(card_copy)
        else:
            stripped_scoring.append(card)

    stripped_played = played_cards
    if enhanced_count > 0 and played_cards is not None:
        stripped_played = [
            orig_to_stripped.get(id(c), c) for c in played_cards
        ]

    vampire_xmult = current_xmult + extra * enhanced_count if enhanced_count > 0 else current_xmult
    return stripped_scoring, stripped_played, vampire_xmult


def _apply_card_scoring(ctx, scoring_cards, played_cards, jokers, ancient_suit):
    """Score cards in played order with per-card joker effects interleaved.

    In Balatro, each card trigger replays the full per-card sequence:
    base chips -> enhancement mult -> Glass xmult -> edition -> per-card jokers.
    Per-card jokers ("when scored") fire WITHIN each trigger, not after.
    """
    from balatro_bot.joker_effects import retrigger_count
    from balatro_bot.constants import FACE_RANKS, FIBONACCI_RANKS, EVEN_RANKS, ODD_RANKS

    scoring_id_set = set(id(c) for c in scoring_cards)
    scored_in_play_order = [c for c in (played_cards or scoring_cards) if id(c) in scoring_id_set]
    played_id_set = set(id(c) for c in scored_in_play_order)
    for c in scoring_cards:
        if id(c) not in played_id_set:
            scored_in_play_order.append(c)

    _per_card = []
    _first_face_found = False
    for j in (jokers or []):
        if is_joker_debuffed(j):
            continue
        k = j.get("key", "")
        ab = j.get("value", {}).get("ability", {})
        if k == "j_greedy_joker":
            _per_card.append(("suit_mult", "D", ab.get("s_mult", 3)))
        elif k == "j_lusty_joker":
            _per_card.append(("suit_mult", "H", ab.get("s_mult", 3)))
        elif k == "j_wrathful_joker":
            _per_card.append(("suit_mult", "S", ab.get("s_mult", 3)))
        elif k == "j_gluttenous_joker":
            _per_card.append(("suit_mult", "C", ab.get("s_mult", 3)))
        elif k == "j_fibonacci":
            _per_card.append(("ranks_mult", FIBONACCI_RANKS, ab.get("extra", 8)))
        elif k == "j_even_steven":
            _per_card.append(("ranks_mult", EVEN_RANKS, ab.get("extra", 4)))
        elif k == "j_odd_todd":
            _per_card.append(("ranks_chips", ODD_RANKS, ab.get("extra", 31)))
        elif k == "j_scholar":
            _per_card.append(("ranks_cm", frozenset({"A"}), ab.get("chips", 20), ab.get("mult", 4)))
        elif k == "j_smiley":
            _per_card.append(("face_mult", ab.get("extra", 5)))
        elif k == "j_scary_face":
            _per_card.append(("face_chips", ab.get("extra", 30)))
        elif k == "j_walkie_talkie":
            _per_card.append(("ranks_cm", frozenset({"T", "4"}), ab.get("chips", 10), ab.get("mult", 4)))
        elif k == "j_photograph":
            _per_card.append(("first_face_xmult", ab.get("extra", 2.0)))
        elif k == "j_triboulet":
            _per_card.append(("ranks_xmult", frozenset({"K", "Q"}), ab.get("extra", 2.0)))
        elif k == "j_ancient":
            _per_card.append(("suit_xmult", ancient_suit, 1.5))
        elif k == "j_arrowhead":
            _per_card.append(("suit_chips", "S", ab.get("extra", 50)))
        elif k == "j_onyx_agate":
            _per_card.append(("suit_mult", "C", ab.get("extra", 7)))
        elif k == "j_bloodstone":
            xm = ab.get("Xmult", 1.5)
            odds = ab.get("odds", 2)
            _per_card.append(("suit_expected_xmult", "H", xm, odds))
        elif k == "j_hiker":
            _per_card.append(("all_chips", ab.get("extra", 5)))

    _has_midas = any(j.get("key") == "j_midas_mask" for j in (jokers or []))
    if _has_midas:
        for i, card in enumerate(scored_in_play_order):
            rank = card_rank(card)
            if rank and (ctx.pareidolia or rank in FACE_RANKS):
                mod = _modifier(card)
                if isinstance(mod, dict) and mod.get("enhancement") not in (None, "", "GOLD"):
                    card_copy = dict(card)
                    mod_copy = dict(mod)
                    mod_copy["enhancement"] = "GOLD"
                    card_copy["modifier"] = mod_copy
                    scored_in_play_order[i] = card_copy

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

    _baron_xm = 0.0
    _shoot_moon_mult = 0.0
    _has_mime = False
    for j in (jokers or []):
        k = j.get("key", "")
        if k == "j_baron":
            from balatro_bot.joker_effects.parsers import _ability as _ab
            _baron_xm = _ab(j).get("extra", 1.5)
        elif k == "j_shoot_the_moon":
            from balatro_bot.joker_effects.parsers import _ability as _ab
            _shoot_moon_mult = _ab(j).get("extra", 13)
        elif k == "j_mime":
            _has_mime = True

    _held_triggers = 2 if _has_mime else 1
    for card in ctx.held_cards:
        if not is_debuffed(card):
            for _ in range(_held_triggers):
                if _modifier(card).get("enhancement") == "STEEL":
                    ctx.mult *= 1.5
                if _baron_xm and card_rank(card) == "K":
                    ctx.mult *= _baron_xm
                if _shoot_moon_mult and card_rank(card) == "Q":
                    ctx.mult += _shoot_moon_mult


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

    joker_keys_set = {j.get("key") for j in jokers if not is_joker_debuffed(j)}
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
    )

    effective_scoring, effective_played, vampire_xmult = _apply_before_phase(
        scoring_cards, played_cards, jokers)
    ctx.vampire_xmult = vampire_xmult

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
) -> dict:
    """Like score_hand but returns a full breakdown dict for logging."""
    from balatro_bot.joker_effects import ScoreContext, apply_joker_effects_detailed, retrigger_count

    base_chips, base_mult, _ = HAND_INFO[hand_name]
    if hand_levels and hand_name in hand_levels:
        lvl = hand_levels[hand_name]
        base_chips = lvl.get("chips", base_chips)
        base_mult = lvl.get("mult", base_mult)

    card_details = []
    for c in scoring_cards:
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

    joker_keys_set = {j.get("key") for j in jokers if not is_joker_debuffed(j)}
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
    )

    effective_scoring, effective_played, vampire_xmult = _apply_before_phase(
        scoring_cards, played_cards, jokers)
    ctx.vampire_xmult = vampire_xmult

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
