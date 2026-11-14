"""A joker's current values, computed the way the game displays them.

The game shows each joker's live numbers in its description
(``Card:generate_UIBox_ability_table``, card.lua:708-948). Some are stored in
``card.ability``; others it computes from game-wide state as it draws the card.
``enrich_state`` writes the result to each joker's ``value.current``, so the bot
reads numbers instead of description text. Inputs are the joker's
``value.ability`` and the gamestate's ``counters`` block; line numbers cite
vanilla card.lua (Steamodded leaves these lines unchanged).

``current`` keys: ``chips``, ``mult``, ``xmult``, ``dollars`` (what the joker
contributes when it fires) and ``counter`` (a countdown the description shows).
"""

from __future__ import annotations

from typing import Any, Callable

# Every joker area whose cards carry a description in the gamestate.
_JOKER_AREAS = ("jokers", "shop", "pack")

Values = dict[str, float]
Formula = Callable[[dict, dict, dict], Values]


def _num(ab: dict, key: str, default: float = 0.0) -> float:
    # The fork omits ability.mult / x_mult / chips when they are 0, so absent means 0.
    v = ab.get(key)
    return float(v) if isinstance(v, (int, float)) else default


def _stored_mult(ab: dict, state: dict, counters: dict) -> Values:
    return {"mult": _num(ab, "mult")}


def _stored_xmult(ab: dict, state: dict, counters: dict) -> Values:
    return {"xmult": _num(ab, "x_mult", 1.0)}


def _extra_chips(ab: dict, state: dict, counters: dict) -> Values:
    return {"chips": _num(ab, "chips")}


def _extra_xmult(ab: dict, state: dict, counters: dict) -> Values:
    return {"xmult": _num(ab, "Xmult", 1.0)}


def _from(field: str, kind: str, default: float = 0.0) -> Formula:
    """A fixed value the description shows straight from one ability field."""
    def formula(ab: dict, state: dict, counters: dict) -> Values:
        return {kind: _num(ab, field, default)}
    return formula


def _counter_value(counters: dict, key: str) -> float | None:
    v = counters.get(key)
    return float(v) if isinstance(v, (int, float)) else None


def _fortune_teller(ab: dict, state: dict, counters: dict) -> Values:
    tarots = _counter_value(counters, "tarots_used")
    return {} if tarots is None else {"mult": _num(ab, "extra") * tarots}  # card.lua:743


def _steel_joker(ab: dict, state: dict, counters: dict) -> Values:
    return {"xmult": 1 + _num(ab, "extra") * _num(ab, "steel_tally")}  # card.lua:744


def _stone_joker(ab: dict, state: dict, counters: dict) -> Values:
    return {"chips": _num(ab, "extra") * _num(ab, "stone_tally")}  # card.lua:747


def _blue_joker(ab: dict, state: dict, counters: dict) -> Values:
    deck = (state.get("cards") or {}).get("cards")
    if not isinstance(deck, list):
        return {}
    return {"chips": _num(ab, "extra") * len(deck)}  # card.lua:753, #G.deck.cards


def _abstract(ab: dict, state: dict, counters: dict) -> Values:
    jokers = (state.get("jokers") or {}).get("cards") or []
    return {"mult": len(jokers) * _num(ab, "extra")}  # card.lua:788, #G.jokers.cards


def _erosion(ab: dict, state: dict, counters: dict) -> Values:
    start = _counter_value(counters, "starting_deck_size")
    owned = _counter_value(counters, "playing_cards")
    if start is None or owned is None:
        return {}
    return {"mult": max(0.0, _num(ab, "extra") * (start - owned))}  # card.lua:878


def _bull(ab: dict, state: dict, counters: dict) -> Values:
    return {"chips": _num(ab, "extra") * max(0, state.get("money", 0) or 0)}  # card.lua:885


def _bootstraps(ab: dict, state: dict, counters: dict) -> Values:
    # card.lua:915 also adds G.GAME.dollar_buffer, which is non-zero only mid-scoring.
    step = _num(ab, "dollars", 5.0) or 5.0
    return {"mult": _num(ab, "mult") * ((state.get("money", 0) or 0) // step)}


def _caino(ab: dict, state: dict, counters: dict) -> Values:
    return {"xmult": _num(ab, "caino_xmult", 1.0)}  # card.lua:916


def _yorick(ab: dict, state: dict, counters: dict) -> Values:
    out: Values = {"xmult": _num(ab, "x_mult", 1.0)}  # card.lua:918
    if isinstance(ab.get("yorick_discards"), (int, float)):
        out["counter"] = float(ab["yorick_discards"])
    return out


def _cloud_9(ab: dict, state: dict, counters: dict) -> Values:
    return {"dollars": _num(ab, "extra") * _num(ab, "nine_tally")}  # card.lua:859


def _satellite(ab: dict, state: dict, counters: dict) -> Values:
    planets = _counter_value(counters, "planets_used_unique")
    return {} if planets is None else {"dollars": _num(ab, "extra") * planets}  # card.lua:908-911


def _invisible(ab: dict, state: dict, counters: dict) -> Values:
    rounds = ab.get("invis_rounds")
    return {} if not isinstance(rounds, (int, float)) else {"counter": float(rounds)}  # card.lua:898


def _loyalty(ab: dict, state: dict, counters: dict) -> Values:
    out: Values = {"xmult": _num(ab, "Xmult", 1.0)}  # card.lua:782
    if isinstance(ab.get("loyalty_remaining"), (int, float)):
        out["counter"] = float(ab["loyalty_remaining"])
    return out


FORMULAS: dict[str, Formula] = {
    # running mult stored in ability.mult
    "j_joker": _stored_mult,            # card.lua:733
    "j_ceremonial": _stored_mult,       # card.lua:764
    "j_green_joker": _stored_mult,      # card.lua:749
    "j_trousers": _stored_mult,         # card.lua:796
    "j_ride_the_bus": _stored_mult,     # card.lua:798
    "j_swashbuckler": _stored_mult,     # card.lua:826
    "j_red_card": _stored_mult,         # card.lua:849
    "j_flash": _stored_mult,            # card.lua:888
    "j_popcorn": _stored_mult,          # card.lua:889
    # running xmult stored in ability.x_mult
    "j_stencil": _stored_xmult,         # card.lua:762
    "j_constellation": _stored_xmult,   # card.lua:806
    "j_throwback": _stored_xmult,       # card.lua:829
    "j_glass": _stored_xmult,           # card.lua:835
    "j_hit_the_road": _stored_xmult,    # card.lua:843
    "j_madness": _stored_xmult,         # card.lua:850
    "j_vampire": _stored_xmult,         # card.lua:854
    "j_hologram": _stored_xmult,        # card.lua:856
    "j_obelisk": _stored_xmult,         # card.lua:861
    "j_lucky_cat": _stored_xmult,       # card.lua:883
    "j_ramen": _stored_xmult,           # card.lua:890
    "j_campfire": _stored_xmult,        # card.lua:896
    "j_duo": _stored_xmult,             # card.lua:844-845
    "j_trio": _stored_xmult,
    "j_family": _stored_xmult,
    "j_order": _stored_xmult,
    "j_tribe": _stored_xmult,
    # chips stored in ability.extra.chips
    "j_runner": _extra_chips,           # card.lua:802
    "j_ice_cream": _extra_chips,        # card.lua:803
    "j_wee": _extra_chips,              # card.lua:838
    "j_square": _extra_chips,           # card.lua:851
    "j_castle": _extra_chips,           # card.lua:894
    # xmult stored in ability.extra.Xmult
    "j_cavendish": _extra_xmult,        # card.lua:847
    "j_card_sharp": _extra_xmult,       # card.lua:848
    "j_bloodstone": _extra_xmult,       # card.lua:832
    "j_loyalty_card": _loyalty,
    # fixed values the description reads from ability.extra / s_mult / t_mult / t_chips
    "j_blackboard": _from("extra", "xmult", 1.0),      # card.lua:801
    "j_acrobat": _from("extra", "xmult", 1.0),         # card.lua:824
    "j_flower_pot": _from("extra", "xmult", 1.0),      # card.lua:837
    "j_seeing_double": _from("extra", "xmult", 1.0),   # card.lua:841
    "j_baron": _from("extra", "xmult", 1.0),           # card.lua:858
    "j_photograph": _from("extra", "xmult", 1.0),      # card.lua:875
    "j_baseball": _from("extra", "xmult", 1.0),        # card.lua:884
    "j_triboulet": _from("extra", "xmult", 1.0),       # card.lua:917
    "j_idol": _from("extra", "xmult", 1.0),            # card.lua:840
    "j_ancient": _from("extra", "xmult", 1.0),         # card.lua:891
    "j_drivers_license": _from("extra", "xmult", 1.0), # card.lua:913
    "j_fibonacci": _from("extra", "mult"),             # card.lua:786
    "j_even_steven": _from("extra", "mult"),           # card.lua:791
    "j_onyx_agate": _from("extra", "mult"),            # card.lua:834
    "j_smiley": _from("extra", "mult"),                # card.lua:895
    "j_shoot_the_moon": _from("extra", "mult"),        # card.lua:912
    "j_greedy_joker": _from("s_mult", "mult"),         # card.lua:751-752
    "j_lusty_joker": _from("s_mult", "mult"),
    "j_wrathful_joker": _from("s_mult", "mult"),
    "j_gluttenous_joker": _from("s_mult", "mult"),
    "j_jolly": _from("t_mult", "mult"),                # card.lua:734-737
    "j_zany": _from("t_mult", "mult"),
    "j_mad": _from("t_mult", "mult"),
    "j_crazy": _from("t_mult", "mult"),
    "j_droll": _from("t_mult", "mult"),
    "j_sly": _from("t_chips", "chips"),                # card.lua:738-741
    "j_wily": _from("t_chips", "chips"),
    "j_clever": _from("t_chips", "chips"),
    "j_devious": _from("t_chips", "chips"),
    "j_crafty": _from("t_chips", "chips"),
    "j_banner": _from("extra", "chips"),               # card.lua:765, per remaining discard
    "j_scary_face": _from("extra", "chips"),           # card.lua:787
    "j_odd_todd": _from("extra", "chips"),             # card.lua:792
    "j_arrowhead": _from("extra", "chips"),            # card.lua:833
    "j_hiker": _from("extra", "chips"),                # card.lua:807
    # computed from game-wide state as the description is drawn
    "j_fortune_teller": _fortune_teller,
    "j_steel_joker": _steel_joker,
    "j_stone": _stone_joker,
    "j_blue_joker": _blue_joker,
    "j_abstract": _abstract,
    "j_erosion": _erosion,
    "j_bull": _bull,
    "j_bootstraps": _bootstraps,
    "j_caino": _caino,
    "j_yorick": _yorick,
    "j_cloud_9": _cloud_9,
    "j_satellite": _satellite,
    "j_invisible": _invisible,
}


def current_values(joker: dict, state: dict) -> Values:
    """The joker's current values in ``state``; empty when the game shows none."""
    formula = FORMULAS.get(joker.get("key", ""))
    if formula is None:
        return {}
    value = joker.get("value")
    ab = value.get("ability") if isinstance(value, dict) else None
    if not isinstance(ab, dict):
        ab = {}
    counters = state.get("counters")
    return formula(ab, state, counters if isinstance(counters, dict) else {})


def enrich_state(state: Any) -> Any:
    """Write ``value.current`` onto every joker in a gamestate dict, in place."""
    if not isinstance(state, dict):
        return state
    for area_name in _JOKER_AREAS:
        area = state.get(area_name)
        if not isinstance(area, dict):
            continue
        for card in area.get("cards") or []:
            if not isinstance(card, dict) or not str(card.get("key", "")).startswith("j_"):
                continue
            if not isinstance(card.get("value"), dict):
                card["value"] = {}
            card["value"]["current"] = current_values(card, state)
    return state
