"""Joker current values: each formula against the game's description code (vanilla card.lua)."""

from __future__ import annotations

import pytest

from balatro_bot.joker_effects.live_values import enrich_state
from balatro_bot.joker_effects.parsers import _ab_chips, _ab_mult, _ab_xmult, current_counter


def _current(key: str, ability: dict, **state) -> dict:
    j = {"key": key, "value": {"ability": ability}}
    enrich_state({**state, "jokers": {"cards": [j, *state.get("_others", [])]}})
    return j["value"]["current"]


def _cards(n: int) -> dict:
    return {"cards": [{"key": f"H_{i}"} for i in range(n)]}


class TestComputedFromGameState:
    def test_blue_joker_counts_draw_pile(self):
        # card.lua:753 — extra * #G.deck.cards
        assert _current("j_blue_joker", {"extra": 2}, cards=_cards(37)) == {"chips": 74}

    def test_abstract_counts_owned_jokers(self):
        # card.lua:788 — #G.jokers.cards * extra; the Abstract itself counts
        others = [{"key": "j_joker"}, {"key": "j_misprint"}]
        assert _current("j_abstract", {"extra": 3}, _others=others) == {"mult": 9}

    def test_erosion_counts_cards_below_starting_deck(self):
        # card.lua:878 — max(0, extra * (starting_deck_size - #G.playing_cards))
        counters = {"starting_deck_size": 52, "playing_cards": 47}
        assert _current("j_erosion", {"extra": 4}, counters=counters) == {"mult": 20}

    def test_erosion_never_negative(self):
        counters = {"starting_deck_size": 52, "playing_cards": 60}
        assert _current("j_erosion", {"extra": 4}, counters=counters) == {"mult": 0}

    def test_bull_uses_dollars(self):
        # card.lua:885 — extra * max(0, dollars)
        assert _current("j_bull", {"extra": 2}, money=23) == {"chips": 46}
        assert _current("j_bull", {"extra": 2}, money=-5) == {"chips": 0}

    def test_bootstraps_per_five_dollars(self):
        # card.lua:915 — mult * floor(dollars / 5)
        assert _current("j_bootstraps", {"mult": 2, "dollars": 5}, money=23) == {"mult": 8}

    def test_fortune_teller_counts_tarots_used(self):
        # card.lua:743 — extra * consumeable_usage_total.tarot
        assert _current("j_fortune_teller", {"extra": 1}, counters={"tarots_used": 6}) == {"mult": 6}

    def test_satellite_counts_unique_planets(self):
        # card.lua:908-911 — extra * planets used (unique keys)
        assert _current("j_satellite", {"extra": 1}, counters={"planets_used_unique": 4}) == {"dollars": 4}

    def test_missing_counters_leave_no_value(self):
        assert _current("j_erosion", {"extra": 4}) == {}
        assert _current("j_fortune_teller", {"extra": 1}) == {}
        assert _current("j_satellite", {"extra": 1}) == {}


class TestTalliesAndCounters:
    def test_steel_joker_from_tally(self):
        # card.lua:744 — 1 + extra * steel_tally
        assert _current("j_steel_joker", {"extra": 0.2, "steel_tally": 3}) == pytest.approx({"xmult": 1.6})

    def test_stone_joker_from_tally(self):
        # card.lua:747 — extra * stone_tally
        assert _current("j_stone", {"extra": 25, "stone_tally": 2}) == {"chips": 50}

    def test_cloud_9_from_tally(self):
        # card.lua:859 — extra * nine_tally
        assert _current("j_cloud_9", {"extra": 1, "nine_tally": 4}) == {"dollars": 4}

    def test_yorick_counter_and_xmult(self):
        # card.lua:918 — x_mult and yorick_discards
        j = {"key": "j_yorick", "value": {"ability": {"x_mult": 2, "yorick_discards": 9}}}
        enrich_state({"jokers": {"cards": [j]}})
        assert j["value"]["current"] == {"xmult": 2, "counter": 9}
        assert current_counter(j) == 9

    def test_caino_reads_caino_xmult(self):
        # card.lua:916 — caino_xmult, not x_mult
        assert _current("j_caino", {"caino_xmult": 3, "x_mult": 1}) == {"xmult": 3}


class TestStoredValues:
    def test_zero_mult_omitted_by_the_fork_reads_zero(self):
        # The fork drops ability.mult when it is 0; Ride the Bus reset to 0 stays 0.
        assert _current("j_ride_the_bus", {"extra": 1}) == {"mult": 0}

    def test_scaling_xmult(self):
        assert _current("j_obelisk", {"extra": 0.2, "x_mult": 2.4}) == {"xmult": 2.4}

    def test_extra_chips(self):
        assert _current("j_castle", {"chips": 45, "chip_mod": 3}) == {"chips": 45}

    def test_fixed_xmult_in_extra(self):
        # card.lua:801 — Blackboard shows X(extra)
        assert _current("j_blackboard", {"extra": 3, "x_mult": 1}) == {"xmult": 3}

    def test_suit_mult(self):
        # card.lua:751-752 — Greedy & co. show +s_mult
        assert _current("j_greedy_joker", {"s_mult": 3, "suit": "Diamonds"}) == {"mult": 3}


class TestEnrichState:
    def test_enriches_shop_and_pack_jokers(self):
        state = {
            "shop": {"cards": [{"key": "j_bull", "value": {"ability": {"extra": 2}}}]},
            "pack": {"cards": [{"key": "j_stone", "value": {"ability": {"extra": 25, "stone_tally": 1}}}]},
            "money": 10,
        }
        enrich_state(state)
        assert state["shop"]["cards"][0]["value"]["current"] == {"chips": 20}
        assert state["pack"]["cards"][0]["value"]["current"] == {"chips": 25}

    def test_leaves_non_jokers_alone(self):
        state = {"shop": {"cards": [{"key": "c_fool", "value": {}}]}}
        enrich_state(state)
        assert "current" not in state["shop"]["cards"][0]["value"]

    def test_unknown_joker_gets_empty_current(self):
        state = {"jokers": {"cards": [{"key": "j_dna", "value": {}}]}}
        enrich_state(state)
        assert state["jokers"]["cards"][0]["value"]["current"] == {}

    def test_non_dict_results_pass_through(self):
        assert enrich_state(None) is None
        assert enrich_state({"status": "ok"}) == {"status": "ok"}

    def test_api_empty_value_list_is_replaced(self):
        # balatrobot sends [] for empty objects
        state = {"jokers": {"cards": [{"key": "j_bull", "value": []}]}, "money": 3}
        enrich_state(state)
        assert state["jokers"]["cards"][0]["value"]["current"] == {"chips": 0}


class TestAccessorsPreferCurrent:
    def test_current_beats_ability(self):
        j = {"key": "j_x", "value": {"current": {"mult": 7, "chips": 5, "xmult": 2},
                                      "ability": {"mult": 1, "chips": 1, "x_mult": 1}}}
        assert (_ab_mult(j), _ab_chips(j), _ab_xmult(j)) == (7, 5, 2)

    def test_ability_then_fallback(self):
        j = {"key": "j_x", "value": {"ability": {"mult": 4}}}
        assert _ab_mult(j) == 4
        assert _ab_chips(j, fallback=9) == 9

    def test_effect_text_is_not_read(self):
        j = {"key": "j_x", "value": {"effect": "(Currently X4.5 Mult)"}}
        assert _ab_xmult(j, fallback=1.0) == 1.0
