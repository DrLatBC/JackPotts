"""Shared per-card joker effect model.

The game fires "when scored" jokers (Fibonacci, Smiley, Photograph, Triboulet,
Idol, Bloodstone, etc.) inside a per-scored-card loop — each joker checks the
card and returns chips/mult/xmult contributions for that card alone.

The play-order sequencer (domain.scoring.play_order) reasons about these
contributions: the builder turns a joker list into a flat effects list, and the
contribution helper reports what one card would earn per trigger under that
list. Scoring itself is the compiled kernel's (kernel/core/src/jokers.rs).
"""

from __future__ import annotations

from dataclasses import dataclass

from balatro_bot.cards import (
    card_rank,
    card_suits,
    is_debuffed,
)
from balatro_bot.constants import EVEN_RANKS, FACE_RANKS, FIBONACCI_RANKS, ODD_RANKS
from balatro_bot.joker_effects.parsers import _ability
from balatro_bot.joker_effects.roster import Roster, roster


@dataclass(frozen=True)
class PerCardCtx:
    smeared: bool = False
    pareidolia: bool = False
    idol_rank: str | None = None
    idol_suit: str | None = None
    ancient_suit: str | None = None


PerCardEffect = tuple  # ("<kind>", *args)


class PerCardPlan:
    """One lineup's per-card effects under one PerCardCtx."""

    __slots__ = ("effects", "ctx")

    def __init__(self, effects: tuple[PerCardEffect, ...], ctx: PerCardCtx) -> None:
        self.effects = effects
        self.ctx = ctx


def per_card_plan(
    lineup: Roster,
    smeared: bool = False,
    pareidolia: bool = False,
    idol_rank: str | None = None,
    idol_suit: str | None = None,
    ancient_suit: str | None = None,
) -> PerCardPlan:
    """The lineup's PerCardPlan for these settings, built once per lineup."""
    key = ("per_card", smeared, pareidolia, idol_rank, idol_suit, ancient_suit)
    plan = lineup.derived.get(key)
    if plan is None:
        ctx = PerCardCtx(smeared, pareidolia, idol_rank, idol_suit, ancient_suit)
        plan = lineup.derived[key] = PerCardPlan(_build(lineup, ctx), ctx)
    return plan


def build_per_card_effects(
    jokers: list[dict] | None,
    ctx: PerCardCtx,
    lineup: Roster | None = None,
) -> tuple[PerCardEffect, ...]:
    """Walk the joker roster and emit per-card effect tuples.

    Blueprint / Brainstorm are resolved to their copy targets. Debuffed jokers
    (and debuffed copy targets) are skipped. *lineup* is ``roster(jokers)`` when
    the caller already has it.
    """
    return per_card_plan(
        lineup or roster(jokers),
        ctx.smeared, ctx.pareidolia, ctx.idol_rank, ctx.idol_suit, ctx.ancient_suit,
    ).effects


def _build(lineup: Roster, ctx: PerCardCtx) -> tuple[PerCardEffect, ...]:
    effects: list[PerCardEffect] = []

    def _add(key: str, joker: dict) -> None:
        ab = _ability(joker)
        if key == "j_greedy_joker":
            effects.append(("suit_mult", "D", ab.get("s_mult", 3)))
        elif key == "j_lusty_joker":
            effects.append(("suit_mult", "H", ab.get("s_mult", 3)))
        elif key == "j_wrathful_joker":
            effects.append(("suit_mult", "S", ab.get("s_mult", 3)))
        elif key == "j_gluttenous_joker":
            effects.append(("suit_mult", "C", ab.get("s_mult", 3)))
        elif key == "j_fibonacci":
            effects.append(("ranks_mult", FIBONACCI_RANKS, ab.get("extra", 8)))
        elif key == "j_even_steven":
            effects.append(("ranks_mult", EVEN_RANKS, ab.get("extra", 4)))
        elif key == "j_odd_todd":
            effects.append(("ranks_chips", ODD_RANKS, ab.get("extra", 31)))
        elif key == "j_scholar":
            effects.append(("ranks_cm", frozenset({"A"}), ab.get("chips", 20), ab.get("mult", 4)))
        elif key == "j_smiley":
            effects.append(("face_mult", ab.get("extra", 5)))
        elif key == "j_scary_face":
            effects.append(("face_chips", ab.get("extra", 30)))
        elif key == "j_walkie_talkie":
            effects.append(("ranks_cm", frozenset({"T", "4"}), ab.get("chips", 10), ab.get("mult", 4)))
        elif key == "j_photograph":
            effects.append(("first_face_xmult", ab.get("extra", 2.0)))
        elif key == "j_triboulet":
            effects.append(("ranks_xmult", frozenset({"K", "Q"}), ab.get("extra", 2.0)))
        elif key == "j_ancient":
            effects.append(("suit_xmult", ctx.ancient_suit, 1.5))
        elif key == "j_arrowhead":
            effects.append(("suit_chips", "S", ab.get("extra", 50)))
        elif key == "j_onyx_agate":
            effects.append(("suit_mult", "C", ab.get("extra", 7)))
        elif key == "j_bloodstone":
            xm = ab.get("Xmult", 1.5)
            odds = ab.get("odds", 2)
            effects.append(("suit_expected_xmult", "H", xm, odds))
        elif key == "j_idol":
            if ctx.idol_rank and ctx.idol_suit:
                effects.append(("rank_suit_xmult", ctx.idol_rank, ctx.idol_suit, ab.get("extra", 2.0)))
        elif key == "j_hiker":
            effects.append(("all_chips", ab.get("extra", 5)))

    for k, j in lineup.resolved:
        _add(k, j)
    return tuple(effects)


def apply_effect_to_card(
    eff: PerCardEffect,
    card: dict,
    rank: str | None,
    suits: set[str],
    ctx: PerCardCtx,
    *,
    is_first_face: bool,
) -> tuple[float, float, float]:
    """Return (chips, mult_add, xmult_mul) for a single effect on a single card.

    Does not check debuff — caller must filter debuffed cards.
    """
    kind = eff[0]
    if kind == "ranks_mult" and rank in eff[1]:
        return 0.0, eff[2], 1.0
    if kind == "ranks_chips" and rank in eff[1]:
        return eff[2], 0.0, 1.0
    if kind == "ranks_cm" and rank in eff[1]:
        return eff[2], eff[3], 1.0
    if kind == "face_mult" and (ctx.pareidolia or rank in FACE_RANKS):
        return 0.0, eff[1], 1.0
    if kind == "face_chips" and (ctx.pareidolia or rank in FACE_RANKS):
        return eff[1], 0.0, 1.0
    if kind == "suit_mult" and eff[1] in suits:
        return 0.0, eff[2], 1.0
    if kind == "suit_chips" and eff[1] in suits:
        return eff[2], 0.0, 1.0
    if kind == "suit_xmult" and eff[1] and eff[1] in suits:
        return 0.0, 0.0, eff[2]
    if kind == "suit_expected_xmult" and eff[1] in suits:
        return 0.0, 0.0, eff[2] ** (1.0 / eff[3])
    if kind == "ranks_xmult" and rank in eff[1]:
        return 0.0, 0.0, eff[2]
    if kind == "rank_suit_xmult" and rank == eff[1] and eff[2] in suits:
        return 0.0, 0.0, eff[3]
    if kind == "first_face_xmult" and is_first_face:
        return 0.0, 0.0, eff[1]
    return 0.0, 0.0, 1.0


def card_contribution(
    card: dict,
    effects: tuple[PerCardEffect, ...],
    ctx: PerCardCtx,
    *,
    is_first_face: bool = False,
) -> tuple[float, float, float]:
    """Return (chips_add, mult_add, xmult) this card earns from per-card
    effects in a single trigger. Excludes card-owned Glass/Polychrome and
    Hiker's cumulative 'all_chips' (callers that need trigger-indexed
    behavior should dispatch via apply_effect_to_card themselves).
    """
    if is_debuffed(card):
        return 0.0, 0.0, 1.0
    rank = card_rank(card)
    suits = card_suits(card, smeared=ctx.smeared) if effects else set()
    chips, mult, xmult = 0.0, 0.0, 1.0
    for eff in effects:
        if eff[0] == "all_chips":
            continue  # Hiker — trigger-indexed, not a simple per-trigger value
        dc, dm, dx = apply_effect_to_card(eff, card, rank, suits, ctx, is_first_face=is_first_face)
        chips += dc
        mult += dm
        xmult *= dx
    return chips, mult, xmult
