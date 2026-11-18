"""Roster order: one table of rosters and the order the bot keeps them in."""

import pytest

from balatro_bot.domain.policy.roster_order import roster_order


def j(key, mult=None, xmult=None, edition=None):
    current = {}
    if mult is not None:
        current["mult"] = mult
    if xmult is not None:
        current["xmult"] = xmult
    card = {"key": key, "label": key[2:], "value": {"current": current}}
    if edition:
        card["modifier"] = {"edition": edition}
    return card


CHIPS = j("j_scary_face")
MULT = j("j_joker", mult=4)
BIG_MULT = j("j_gros_michel", mult=15)
CAV = j("j_cavendish", xmult=3)
CAV_POLY = {**j("j_cavendish", xmult=3, edition="POLYCHROME"), "label": "polychrome cavendish"}
DUO = j("j_duo", xmult=2)
PHOTO = j("j_photograph", xmult=2)
BARON = j("j_baron", xmult=1.5)
BP = j("j_blueprint")
BS = j("j_brainstorm")
DAGGER = j("j_ceremonial", mult=0)
CHICOT = j("j_chicot")
HACK = j("j_hack")


def keys(roster, order):
    return [roster[i]["label"] for i in order]


# (roster, fodder index, blind_next, expected order as labels)
CASES = {
    "phase sort": (
        [CAV, MULT, CHIPS], None, False, ["scary_face", "joker", "cavendish"],
    ),
    "polychrome last in its group": (
        [CAV_POLY, CAV, MULT], None, False, ["joker", "cavendish", "polychrome cavendish"],
    ),
    "blueprint copies the best target, not the rightmost": (
        [BP, CAV, DUO, MULT], None, False, ["joker", "blueprint", "cavendish", "duo"],
    ),
    "blueprint with nothing to copy goes last": (
        [BP, CHICOT, CHIPS], None, False, ["chicot", "scary_face", "blueprint"],
    ),
    "brainstorm takes a per-card xmult first": (
        [BS, CAV, PHOTO, MULT], None, False, ["photograph", "joker", "cavendish", "brainstorm"],
    ),
    "brainstorm takes the best +mult, never a joker-phase xmult": (
        [BS, CAV, MULT, BIG_MULT], None, False, ["gros_michel", "joker", "cavendish", "brainstorm"],
    ),
    "brainstorm with only xmult keeps the phase sort": (
        [BS, CAV, CHICOT], None, False, ["chicot", "cavendish", "brainstorm"],
    ),
    "brainstorm copies through blueprint": (
        [BS, BP, MULT, BARON], None, False, ["blueprint", "baron", "joker", "brainstorm"],
    ),
    "dagger sorts as +mult in a round": (
        [CAV, DAGGER, CHICOT, MULT], None, False, ["chicot", "ceremonial", "joker", "cavendish"],
    ),
    "dagger eats nothing without fodder": (
        [CHICOT, DAGGER, HACK, MULT], None, True, ["chicot", "hack", "joker", "ceremonial"],
    ),
    "dagger sits left of its fodder after the +mult": (
        [CAV, DAGGER, MULT, CHIPS], 3, True, ["joker", "ceremonial", "scary_face", "cavendish"],
    ),
    "blueprint never splits dagger from its fodder": (
        [BP, DAGGER, MULT, CAV], 2, True, ["ceremonial", "joker", "blueprint", "cavendish"],
    ),
    "brainstorm never moves dagger": (
        [BS, DAGGER, CAV], None, True, ["cavendish", "ceremonial", "brainstorm"],
    ),
}


@pytest.mark.parametrize("name", CASES)
def test_order(name):
    roster, fodder, blind_next, expected = CASES[name]
    assert keys(roster, roster_order(roster, fodder, blind_next)) == expected


@pytest.mark.parametrize("name", CASES)
def test_ordering_an_ordered_roster_changes_nothing(name):
    roster, fodder, blind_next, _ = CASES[name]
    order = roster_order(roster, fodder, blind_next)
    placed = [roster[i] for i in order]
    moved_fodder = order.index(fodder) if fodder is not None else None
    assert roster_order(placed, moved_fodder, blind_next) == list(range(len(roster)))


def test_short_rosters():
    assert roster_order([]) == []
    assert roster_order([CAV]) == [0]
