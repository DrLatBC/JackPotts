"""Roster order: the one slot order the bot plays, plans in the shop and prices jokers in."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from balatro_bot.cards import joker_key
from balatro_bot.joker_effects.parsers import _ab_mult, _ab_xmult
from balatro_bot.joker_effects.scoring_phase import (
    PER_CARD_XMULT, PHASE_CHIPS, PHASE_MULT, PHASE_NOOP, PHASE_XMULT,
    get_joker_edition_phase, get_joker_phase,
)
from balatro_bot.scaling import BLUEPRINT_INCOMPATIBLE, CONDITIONAL_XMULT

if TYPE_CHECKING:
    from balatro_bot.cards import JokerLike

__all__ = ["roster_order", "describe_order", "copy_target_score"]

_COPIERS = ("j_blueprint", "j_brainstorm")

# A copier fires on every hand, a hand-type xmult only on its hand type.
_CONDITIONAL_XMULT_DISCOUNT = 0.5


def copy_target_score(j: JokerLike) -> float:
    """How good a Blueprint/Brainstorm target this joker is; above 0 means worth copying.

    Any xmult (100 + its current, discounted if conditional) outranks every
    flat mult (its current mult); copy-incompatible jokers score -1.
    """
    key = joker_key(j)
    if key in BLUEPRINT_INCOMPATIBLE:
        return -1.0
    phase = get_joker_phase(key)
    if phase == PHASE_XMULT:
        xm = _ab_xmult(j, fallback=1.5)
        if key in CONDITIONAL_XMULT:
            xm *= _CONDITIONAL_XMULT_DISCOUNT
        return 100.0 + xm
    if phase == PHASE_MULT:
        return _ab_mult(j, fallback=0.0)
    return 0.0


def roster_order(
    jokers: Sequence[JokerLike],
    fodder: int | None = None,
    blind_next: bool = False,
) -> list[int]:
    """The roster's slots in order: ``result[k]`` is the current index of the joker for slot k.

    Scoring jokers sort +chips, +mult, xmult (Polychrome last within its
    group, then current position). Blueprint sits just left of the best copy
    target. Brainstorm goes last and slot 0 takes a per-card xmult, else the
    best +mult or +chips joker; a joker-phase xmult never goes first.

    ``blind_next`` means blind select comes before the next reorder, so
    Ceremonial Dagger eats whatever is right of it: it sits just left of
    ``fodder`` (an index) after the +mult jokers, or last when there is none.
    Otherwise Dagger sorts as +mult.

    Ordering a roster already in this order returns ``range(len(jokers))``.
    """
    n = len(jokers)
    if n < 2:
        return list(range(n))
    keys = [joker_key(j) for j in jokers]
    phase = [get_joker_phase(k) for k in keys]
    blueprints = [i for i in range(n) if keys[i] == "j_blueprint"]
    brainstorms = [i for i in range(n) if keys[i] == "j_brainstorm"]

    order = sorted(
        (i for i in range(n) if keys[i] not in _COPIERS),
        key=lambda i: (phase[i], get_joker_edition_phase(jokers[i]), i),
    )

    # Dagger and its fodder are a pair nothing may split.
    pinned: set[int] = set()
    dagger = next((i for i in order if keys[i] == "j_ceremonial"), None)
    if dagger is not None and blind_next:
        order.remove(dagger)
        pinned = {dagger}
        if fodder is not None and fodder != dagger and fodder in order:
            order.remove(fodder)
            at = sum(1 for i in order if phase[i] <= PHASE_MULT)
            order[at:at] = [dagger, fodder]
            pinned.add(fodder)
        else:
            order.append(dagger)

    def best(cands: list[int]) -> int | None:
        scored = [(copy_target_score(jokers[i]), -pos, i) for pos, i in enumerate(order) if i in cands]
        top = max(scored, default=None)
        return top[2] if top and top[0] > 0 else None

    # Blueprint copies its right neighbour; several Blueprints in a row all copy the target.
    head = None
    if blueprints:
        target = best([i for i in order if i != fodder])
        if target is None:
            order.extend(blueprints)
        else:
            at = order.index(target)
            order[at:at] = blueprints
            head = (blueprints, target)

    if brainstorms:
        free = [i for i in order if i not in pinned and i not in blueprints]
        first = best([i for i in free if keys[i] in PER_CARD_XMULT])
        if first is None:
            first = best([i for i in free if phase[i] == PHASE_MULT])
        if first is None:
            first = next((i for i in free if phase[i] == PHASE_CHIPS and copy_target_score(jokers[i]) >= 0), None)
        if first is not None:
            block = [*head[0], first] if head and head[1] == first else [first]
            order = block + [i for i in order if i not in block]
        order.extend(brainstorms)
    return order


_PHASE_TAGS = {PHASE_NOOP: "noop", PHASE_CHIPS: "+c", PHASE_MULT: "+m", PHASE_XMULT: "xm"}


def describe_order(jokers: Sequence[JokerLike], order: Sequence[int]) -> str:
    """Log text for a roster order: each joker's label and scoring phase."""
    return " ".join(
        f"{jokers[i].get('label', '?')}({_PHASE_TAGS.get(get_joker_phase(joker_key(jokers[i])), '?')})"
        for i in order
    )
