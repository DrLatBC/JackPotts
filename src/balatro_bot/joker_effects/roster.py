"""Per-lineup joker facts that scoring reads for every candidate hand.

Hand search scores hundreds of hands against one joker lineup, so the lineup's
keys, active jokers and Blueprint/Brainstorm targets are worked out once per
lineup instead of once per hand.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from balatro_bot.cards import is_joker_debuffed, joker_key
from balatro_bot.domain.models.joker import Joker
from balatro_bot.joker_effects.parsers import _ability
from balatro_bot.scaling import BLUEPRINT_INCOMPATIBLE

if TYPE_CHECKING:
    from typing import Any

    JokerLike = Joker | dict[str, Any]

_CACHE_MAX = 64


@dataclass(frozen=True)
class Roster:
    keys: frozenset[str]                         # every joker, debuffed included
    active: tuple[tuple[str, JokerLike], ...]    # (key, joker) per non-debuffed joker, in order
    active_keys: frozenset[str]
    resolved: tuple[tuple[str, JokerLike], ...]  # active, with Blueprint/Brainstorm as their copy target
    baseball_xmult: float                        # Baseball Card's xmult, 0 when not owned
    # Further per-lineup facts, each built on first use by the code that reads it
    # (effect plan, held-card jokers, retrigger flags, per-card scoring steps).
    derived: dict = field(default_factory=dict, compare=False, repr=False)


_ROSTERS: dict = {}


def roster(jokers: list | None) -> Roster:
    """The lineup's Roster, memoised on the jokers' identities (in order).

    Joker objects are frozen, so the same objects in the same order always give
    the same result. Each entry keeps its jokers alive so their ids can't be
    reused by other objects while the entry exists, which also means a hit can
    only be a list of the Joker objects that were stored. Raw joker dicts are
    mutable and are never stored: a caller scoring many hands against one list
    of dicts builds the Roster once and passes it down.
    """
    joker_list = jokers or []
    key = tuple(map(id, joker_list))
    hit = _ROSTERS.get(key)
    if hit is not None:
        return hit[1]
    value = _build(joker_list)
    if not all(isinstance(j, Joker) for j in joker_list):
        return value
    if len(_ROSTERS) >= _CACHE_MAX:
        _ROSTERS.clear()
    _ROSTERS[key] = (tuple(joker_list), value)
    return value


def _build(joker_list: list) -> Roster:
    active = []
    resolved = []
    baseball_xmult = None
    for i, j in enumerate(joker_list):
        k = joker_key(j)
        if k == "j_baseball" and baseball_xmult is None:
            baseball_xmult = _ability(j).get("extra", 1.5)
        if is_joker_debuffed(j):
            continue
        active.append((k, j))
        if k == "j_blueprint":
            target = joker_list[i + 1] if i + 1 < len(joker_list) else None
        elif k == "j_brainstorm":
            target = joker_list[0] if joker_list[0] is not j else None
        else:
            resolved.append((k, j))
            continue
        if target is not None and not is_joker_debuffed(target):
            tkey = joker_key(target)
            if tkey not in BLUEPRINT_INCOMPATIBLE:
                resolved.append((tkey, target))
    return Roster(
        keys=frozenset(joker_key(j) for j in joker_list),
        active=tuple(active),
        active_keys=frozenset(k for k, _ in active),
        resolved=tuple(resolved),
        baseball_xmult=baseball_xmult or 0.0,
    )
