"""Scaling projection: each scaling joker's projected end-of-run state."""

from __future__ import annotations

import pytest

from balatro_bot.domain.models.deck_profile import DeckProfile
from balatro_bot.domain.policy.run_view import RunView
from balatro_bot.domain.policy.scaling_projection import project_end_state
from balatro_bot.domain.policy.sim_context import LiveRunStats, SimContext
from tests.conftest import live_joker

_LEVELS = {
    "Pair":      {"chips": 10, "mult": 2, "level": 1},
    "High Card": {"chips": 5,  "mult": 1, "level": 1},
    "Flush":     {"chips": 35, "mult": 4, "level": 1},
}

_DECK = DeckProfile(
    total_cards=52,
    enhancement_counts={"LUCKY": 4, "GLASS": 3, "BONUS": 2},
    enhanced_card_count=9,
)

_LIVE = LiveRunStats(avg_discards_per_round=2.0, avg_sells_per_ante=1.0)

# Ability of an owned joker that has already scaled.
_OWNED_ABILITY = {
    "j_madness": {"x_mult": 2.5},
    "j_throwback": {"x_mult": 2.5},
    "j_caino": {"caino_xmult": 2.0},
    "j_hologram": {"x_mult": 2.5},
    "j_obelisk": {"x_mult": 2.5},
    "j_vampire": {"x_mult": 2.5},
    "j_yorick": {"x_mult": 2.0, "yorick_discards": 7},
    "j_campfire": {"x_mult": 2.5},
    "j_constellation": {"x_mult": 2.5},
    "j_hit_the_road": {"x_mult": 2.5},
    "j_lucky_cat": {"x_mult": 2.5},
    "j_glass": {"x_mult": 2.5},
    "j_green_joker": {"mult": 6},
    "j_supernova": {},
    "j_ride_the_bus": {"mult": 6},
    "j_square": {"chips": 30},
    "j_runner": {"chips": 30},
    "j_trousers": {"mult": 6},
    "j_hiker": {"extra": 5},
    "j_wee": {"chips": 30},
    "j_castle": {"chips": 30},
}

_FRESH_ABILITY = {"j_yorick": {"x_mult": 1, "yorick_discards": 23}}


def _ctx(candidate: dict, roster: list[dict], ante: int, **run) -> SimContext:
    run.setdefault("deck_profile", _DECK)
    run.setdefault("live_stats", _LIVE)
    run.setdefault("unique_planets_used", 3)
    return SimContext.build(candidate, RunView.of(roster, _LEVELS, ante, **run))


def _end_state(key: str, ctx: SimContext) -> dict:
    assert ctx.candidate_key == key
    return project_end_state(ctx)


def _case(key: str, ante: int, owned: bool) -> dict:
    if owned:
        joker = live_joker(key, _OWNED_ABILITY[key])
        return _end_state(key, _ctx(joker, [joker], ante))
    joker = live_joker(key, _FRESH_ABILITY.get(key, {}))
    return _end_state(key, _ctx(joker, [], ante))


# Projected end state per (joker, ante, owned), pinned from the code as it stood
# before the projection inputs moved onto the scaling profile.
GOLDEN: dict[tuple[str, int, bool], dict] = {
    ('j_madness', 1, False): {'xmult': 7.0, 'chips': None, 'mult': None},
    ('j_madness', 1, True): {'xmult': 8.5, 'chips': None, 'mult': None},
    ('j_madness', 4, False): {'xmult': 4.75, 'chips': None, 'mult': None},
    ('j_madness', 4, True): {'xmult': 6.25, 'chips': None, 'mult': None},
    ('j_madness', 8, False): {'xmult': 1.75, 'chips': None, 'mult': None},
    ('j_madness', 8, True): {'xmult': 3.25, 'chips': None, 'mult': None},
    ('j_throwback', 1, False): {'xmult': 1.5, 'chips': None, 'mult': None},
    ('j_throwback', 1, True): {'xmult': 3.0, 'chips': None, 'mult': None},
    ('j_throwback', 4, False): {'xmult': 1.3125, 'chips': None, 'mult': None},
    ('j_throwback', 4, True): {'xmult': 2.8125, 'chips': None, 'mult': None},
    ('j_throwback', 8, False): {'xmult': 1.0625, 'chips': None, 'mult': None},
    ('j_throwback', 8, True): {'xmult': 2.5625, 'chips': None, 'mult': None},
    ('j_caino', 1, False): {'xmult': 1.5, 'chips': None, 'mult': None},
    ('j_caino', 1, True): {'xmult': 2.5, 'chips': None, 'mult': None},
    ('j_caino', 4, False): {'xmult': 1.3125, 'chips': None, 'mult': None},
    ('j_caino', 4, True): {'xmult': 2.3125, 'chips': None, 'mult': None},
    ('j_caino', 8, False): {'xmult': 1.0625, 'chips': None, 'mult': None},
    ('j_caino', 8, True): {'xmult': 2.0625, 'chips': None, 'mult': None},
    ('j_hologram', 1, False): {'xmult': 1.125, 'chips': None, 'mult': None},
    ('j_hologram', 1, True): {'xmult': 2.625, 'chips': None, 'mult': None},
    ('j_hologram', 4, False): {'xmult': 1.078125, 'chips': None, 'mult': None},
    ('j_hologram', 4, True): {'xmult': 2.578125, 'chips': None, 'mult': None},
    ('j_hologram', 8, False): {'xmult': 1.015625, 'chips': None, 'mult': None},
    ('j_hologram', 8, True): {'xmult': 2.515625, 'chips': None, 'mult': None},
    ('j_obelisk', 1, False): {'xmult': 3.4000000000000004, 'chips': None, 'mult': None},
    ('j_obelisk', 1, True): {'xmult': 4.9, 'chips': None, 'mult': None},
    ('j_obelisk', 4, False): {'xmult': 2.5, 'chips': None, 'mult': None},
    ('j_obelisk', 4, True): {'xmult': 4.0, 'chips': None, 'mult': None},
    ('j_obelisk', 8, False): {'xmult': 1.3, 'chips': None, 'mult': None},
    ('j_obelisk', 8, True): {'xmult': 2.8, 'chips': None, 'mult': None},
    ('j_vampire', 1, False): {'xmult': 2.0, 'chips': None, 'mult': None},
    ('j_vampire', 1, True): {'xmult': 3.5, 'chips': None, 'mult': None},
    ('j_vampire', 4, False): {'xmult': 2.0, 'chips': None, 'mult': None},
    ('j_vampire', 4, True): {'xmult': 3.5, 'chips': None, 'mult': None},
    ('j_vampire', 8, False): {'xmult': 1.2336538461538462, 'chips': None, 'mult': None},
    ('j_vampire', 8, True): {'xmult': 2.733653846153846, 'chips': None, 'mult': None},
    ('j_yorick', 1, False): {'xmult': 3.0, 'chips': None, 'mult': None},
    ('j_yorick', 1, True): {'xmult': 4.0, 'chips': None, 'mult': None},
    ('j_yorick', 4, False): {'xmult': 2.0, 'chips': None, 'mult': None},
    ('j_yorick', 4, True): {'xmult': 4.0, 'chips': None, 'mult': None},
    ('j_yorick', 8, False): {'xmult': None, 'chips': None, 'mult': None},
    ('j_yorick', 8, True): {'xmult': 2.0, 'chips': None, 'mult': None},
    ('j_campfire', 1, False): {'xmult': 1.275, 'chips': None, 'mult': None},
    ('j_campfire', 1, True): {'xmult': 2.775, 'chips': None, 'mult': None},
    ('j_campfire', 4, False): {'xmult': 1.275, 'chips': None, 'mult': None},
    ('j_campfire', 4, True): {'xmult': 2.775, 'chips': None, 'mult': None},
    ('j_campfire', 8, False): {'xmult': 1.2, 'chips': None, 'mult': None},
    ('j_campfire', 8, True): {'xmult': 2.7, 'chips': None, 'mult': None},
    ('j_constellation', 1, False): {'xmult': 1.49, 'chips': None, 'mult': None},
    ('j_constellation', 1, True): {'xmult': 2.99, 'chips': None, 'mult': None},
    ('j_constellation', 4, False): {'xmult': 1.30625, 'chips': None, 'mult': None},
    ('j_constellation', 4, True): {'xmult': 2.80625, 'chips': None, 'mult': None},
    ('j_constellation', 8, False): {'xmult': 1.06125, 'chips': None, 'mult': None},
    ('j_constellation', 8, True): {'xmult': 2.56125, 'chips': None, 'mult': None},
    ('j_hit_the_road', 1, False): {'xmult': 1.15, 'chips': None, 'mult': None},
    ('j_hit_the_road', 1, True): {'xmult': 2.65, 'chips': None, 'mult': None},
    ('j_hit_the_road', 4, False): {'xmult': 1.15, 'chips': None, 'mult': None},
    ('j_hit_the_road', 4, True): {'xmult': 2.65, 'chips': None, 'mult': None},
    ('j_hit_the_road', 8, False): {'xmult': 1.15, 'chips': None, 'mult': None},
    ('j_hit_the_road', 8, True): {'xmult': 2.65, 'chips': None, 'mult': None},
    ('j_lucky_cat', 1, False): {'xmult': 2.384615384615385, 'chips': None, 'mult': None},
    ('j_lucky_cat', 1, True): {'xmult': 3.884615384615385, 'chips': None, 'mult': None},
    ('j_lucky_cat', 4, False): {'xmult': 1.8653846153846154, 'chips': None, 'mult': None},
    ('j_lucky_cat', 4, True): {'xmult': 3.3653846153846154, 'chips': None, 'mult': None},
    ('j_lucky_cat', 8, False): {'xmult': 1.1730769230769231, 'chips': None, 'mult': None},
    ('j_lucky_cat', 8, True): {'xmult': 2.673076923076923, 'chips': None, 'mult': None},
    ('j_glass', 1, False): {'xmult': 4.0, 'chips': None, 'mult': None},
    ('j_glass', 1, True): {'xmult': 5.5, 'chips': None, 'mult': None},
    ('j_glass', 4, False): {'xmult': 3.433894230769231, 'chips': None, 'mult': None},
    ('j_glass', 4, True): {'xmult': 4.933894230769231, 'chips': None, 'mult': None},
    ('j_glass', 8, False): {'xmult': 1.4867788461538463, 'chips': None, 'mult': None},
    ('j_glass', 8, True): {'xmult': 2.9867788461538463, 'chips': None, 'mult': None},
    ('j_green_joker', 1, False): {'xmult': None, 'chips': None, 'mult': 36.0},
    ('j_green_joker', 1, True): {'xmult': None, 'chips': None, 'mult': 42.0},
    ('j_green_joker', 4, False): {'xmult': None, 'chips': None, 'mult': 22.5},
    ('j_green_joker', 4, True): {'xmult': None, 'chips': None, 'mult': 28.5},
    ('j_green_joker', 8, False): {'xmult': None, 'chips': None, 'mult': 4.5},
    ('j_green_joker', 8, True): {'xmult': None, 'chips': None, 'mult': 10.5},
    ('j_supernova', 1, False): {'xmult': None, 'chips': None, 'mult': 36.0},
    ('j_supernova', 1, True): {'xmult': None, 'chips': None, 'mult': 36.0},
    ('j_supernova', 4, False): {'xmult': None, 'chips': None, 'mult': 22.5},
    ('j_supernova', 4, True): {'xmult': None, 'chips': None, 'mult': 22.5},
    ('j_supernova', 8, False): {'xmult': None, 'chips': None, 'mult': 4.5},
    ('j_supernova', 8, True): {'xmult': None, 'chips': None, 'mult': 4.5},
    ('j_ride_the_bus', 1, False): {'xmult': None, 'chips': None, 'mult': 21.599999999999998},
    ('j_ride_the_bus', 1, True): {'xmult': None, 'chips': None, 'mult': 27.599999999999998},
    ('j_ride_the_bus', 4, False): {'xmult': None, 'chips': None, 'mult': 13.5},
    ('j_ride_the_bus', 4, True): {'xmult': None, 'chips': None, 'mult': 19.5},
    ('j_ride_the_bus', 8, False): {'xmult': None, 'chips': None, 'mult': 2.6999999999999997},
    ('j_ride_the_bus', 8, True): {'xmult': None, 'chips': None, 'mult': 8.7},
    ('j_square', 1, False): {'xmult': None, 'chips': 43.199999999999996, 'mult': None},
    ('j_square', 1, True): {'xmult': None, 'chips': 73.19999999999999, 'mult': None},
    ('j_square', 4, False): {'xmult': None, 'chips': 27.0, 'mult': None},
    ('j_square', 4, True): {'xmult': None, 'chips': 57.0, 'mult': None},
    ('j_square', 8, False): {'xmult': None, 'chips': 5.3999999999999995, 'mult': None},
    ('j_square', 8, True): {'xmult': None, 'chips': 35.4, 'mult': None},
    ('j_runner', 1, False): {'xmult': None, 'chips': 80.99999999999999, 'mult': None},
    ('j_runner', 1, True): {'xmult': None, 'chips': 110.99999999999999, 'mult': None},
    ('j_runner', 4, False): {'xmult': None, 'chips': 50.625, 'mult': None},
    ('j_runner', 4, True): {'xmult': None, 'chips': 80.625, 'mult': None},
    ('j_runner', 8, False): {'xmult': None, 'chips': 10.124999999999998, 'mult': None},
    ('j_runner', 8, True): {'xmult': None, 'chips': 40.125, 'mult': None},
    ('j_trousers', 1, False): {'xmult': None, 'chips': None, 'mult': 18.0},
    ('j_trousers', 1, True): {'xmult': None, 'chips': None, 'mult': 24.0},
    ('j_trousers', 4, False): {'xmult': None, 'chips': None, 'mult': 11.25},
    ('j_trousers', 4, True): {'xmult': None, 'chips': None, 'mult': 17.25},
    ('j_trousers', 8, False): {'xmult': None, 'chips': None, 'mult': 2.25},
    ('j_trousers', 8, True): {'xmult': None, 'chips': None, 'mult': 8.25},
    ('j_hiker', 1, False): {'xmult': None, 'chips': 900.0, 'mult': None},
    ('j_hiker', 1, True): {'xmult': None, 'chips': 905.0, 'mult': None},
    ('j_hiker', 4, False): {'xmult': None, 'chips': 562.5, 'mult': None},
    ('j_hiker', 4, True): {'xmult': None, 'chips': 567.5, 'mult': None},
    ('j_hiker', 8, False): {'xmult': None, 'chips': 112.5, 'mult': None},
    ('j_hiker', 8, True): {'xmult': None, 'chips': 117.5, 'mult': None},
    ('j_wee', 1, False): {'xmult': None, 'chips': 57.6, 'mult': None},
    ('j_wee', 1, True): {'xmult': None, 'chips': 87.6, 'mult': None},
    ('j_wee', 4, False): {'xmult': None, 'chips': 36.0, 'mult': None},
    ('j_wee', 4, True): {'xmult': None, 'chips': 66.0, 'mult': None},
    ('j_wee', 8, False): {'xmult': None, 'chips': 7.2, 'mult': None},
    ('j_wee', 8, True): {'xmult': None, 'chips': 37.2, 'mult': None},
    ('j_castle', 1, False): {'xmult': None, 'chips': 36.0, 'mult': None},
    ('j_castle', 1, True): {'xmult': None, 'chips': 66.0, 'mult': None},
    ('j_castle', 4, False): {'xmult': None, 'chips': 22.5, 'mult': None},
    ('j_castle', 4, True): {'xmult': None, 'chips': 52.5, 'mult': None},
    ('j_castle', 8, False): {'xmult': None, 'chips': 4.5, 'mult': None},
    ('j_castle', 8, True): {'xmult': None, 'chips': 34.5, 'mult': None},
}


@pytest.mark.parametrize("case", sorted(GOLDEN), ids=lambda c: f"{c[0]}-a{c[1]}-{'owned' if c[2] else 'fresh'}")
def test_projection_matches_pinned(case):
    key, ante, owned = case
    assert _case(key, ante, owned) == GOLDEN[case]


def test_unprojected_joker_has_no_end_state():
    joker = live_joker("j_joker", {"mult": 4})
    assert _end_state("j_joker", _ctx(joker, [], 3)) == {"xmult": None, "chips": None, "mult": None}


def test_anchor_comes_from_the_roster_not_the_candidate():
    owned = live_joker("j_madness", {"x_mult": 3.0})
    fresh = live_joker("j_madness", {})
    # Ante 8: 3 blinds left, gain 0.5 * 3 * 0.5 = 0.75 over the anchor.
    assert _end_state("j_madness", _ctx(fresh, [owned], 8))["xmult"] == 3.75
    assert _end_state("j_madness", _ctx(fresh, [], 8))["xmult"] == 1.75


def test_yorick_counter_sets_procs():
    near = live_joker("j_yorick", {"x_mult": 1, "yorick_discards": 3})
    fresh = live_joker("j_yorick", {"x_mult": 1, "yorick_discards": 23})
    live = LiveRunStats(avg_discards_per_round=1.0, avg_sells_per_ante=1.0)
    # Ante 8: 3 rounds left, 3 projected discards: reaches a proc 3 away, not one 23 away.
    assert _end_state("j_yorick", _ctx(near, [near], 8, live_stats=live))["xmult"] == 2.0
    assert _end_state("j_yorick", _ctx(fresh, [fresh], 8, live_stats=live))["xmult"] is None


@pytest.mark.parametrize("key, enhancement", [
    ("j_vampire", "BONUS"), ("j_lucky_cat", "LUCKY"), ("j_glass", "GLASS"),
])
def test_density_scalers_need_their_enhancement(key, enhancement):
    joker = live_joker(key, {})
    with_it = DeckProfile(total_cards=52, enhancement_counts={enhancement: 4}, enhanced_card_count=4)
    without = DeckProfile(total_cards=52)
    assert _end_state(key, _ctx(joker, [], 4, deck_profile=with_it))["xmult"] > 1.0
    assert _end_state(key, _ctx(joker, [], 4, deck_profile=without))["xmult"] is None


def test_constellation_headroom_shrinks_with_planets_used():
    joker = live_joker("j_constellation", {})
    few = _end_state("j_constellation", _ctx(joker, [], 2, unique_planets_used=1))["xmult"]
    many = _end_state("j_constellation", _ctx(joker, [], 2, unique_planets_used=9))["xmult"]
    capped = _end_state("j_constellation", _ctx(joker, [], 2, unique_planets_used=10))["xmult"]
    assert few > many > 1.0
    assert capped is None


def test_hologram_projects_more_with_an_enabler():
    joker = live_joker("j_hologram", {})
    marble = live_joker("j_marble", {})
    alone = _end_state("j_hologram", _ctx(joker, [], 2))["xmult"]
    enabled = _end_state("j_hologram", _ctx(joker, [marble], 2))["xmult"]
    assert enabled > alone > 1.0


def test_discard_and_sell_rates_drive_their_scalers():
    low = LiveRunStats(avg_discards_per_round=0.5, avg_sells_per_ante=0.2)
    high = LiveRunStats(avg_discards_per_round=3.0, avg_sells_per_ante=3.0)
    castle = live_joker("j_castle", {})
    campfire = live_joker("j_campfire", {})
    assert (_end_state("j_castle", _ctx(castle, [], 2, live_stats=high))["chips"]
            > _end_state("j_castle", _ctx(castle, [], 2, live_stats=low))["chips"])
    assert (_end_state("j_campfire", _ctx(campfire, [], 2, live_stats=high))["xmult"]
            > _end_state("j_campfire", _ctx(campfire, [], 2, live_stats=low))["xmult"])


def test_additive_projection_adds_to_the_live_value():
    fresh = live_joker("j_green_joker", {})
    owned = live_joker("j_green_joker", {"mult": 6})
    gain = _end_state("j_green_joker", _ctx(fresh, [], 4))["mult"]
    assert _end_state("j_green_joker", _ctx(owned, [owned], 4))["mult"] == 6 + gain
