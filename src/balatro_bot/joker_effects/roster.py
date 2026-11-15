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
    active: tuple[tuple[str, JokerLike], ...]    # (key, joker) per non-debuffed joker, in order
    active_keys: frozenset[str]
    resolved: tuple[tuple[str, JokerLike], ...]  # active, with Blueprint/Brainstorm as their copy target
    # One xmult per Baseball Card in effect (each copy by Blueprint/Brainstorm counts);
    # every one of them multiplies after each Uncommon joker.
    baseball_xmults: tuple[float, ...]
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


def copy_target(joker_list: list, index: int) -> JokerLike | None:
    """The joker whose effect the joker at *index* runs: itself, or for Blueprint and
    Brainstorm the joker at the end of the copy chain (Blueprint copies its right
    neighbour, Brainstorm the leftmost joker, and a copy of a copy joker follows
    that one's target). None when the chain ends on nothing, on a debuffed or
    copy-incompatible joker, or loops back on itself.
    """
    for _ in range(len(joker_list) + 1):
        key = joker_key(joker_list[index])
        if key == "j_blueprint":
            index += 1
        elif key == "j_brainstorm" and index != 0:
            index = 0
        elif key == "j_brainstorm":
            return None
        else:
            return joker_list[index]
        if index >= len(joker_list):
            return None
        target = joker_list[index]
        if is_joker_debuffed(target) or joker_key(target) in BLUEPRINT_INCOMPATIBLE:
            return None
    return None


def _build(joker_list: list) -> Roster:
    active = []
    resolved = []
    for i, j in enumerate(joker_list):
        if is_joker_debuffed(j):
            continue
        active.append((joker_key(j), j))
        target = copy_target(joker_list, i)
        if target is not None:
            resolved.append((joker_key(target), target))
    return Roster(
        active=tuple(active),
        active_keys=frozenset(k for k, _ in active),
        resolved=tuple(resolved),
        baseball_xmults=tuple(
            _ability(j).get("extra", 1.5) for k, j in resolved if k == "j_baseball"
        ),
    )
