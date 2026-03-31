"""Data-driven joker effects — simple jokers described by a table, not individual functions."""

from __future__ import annotations

from balatro_bot.joker_effects.parsers import _ability, _ab_chips, _ab_mult, _ab_xmult
from balatro_bot.joker_effects.context import ScoreContext, _count_suit_in_scoring, _hand_contains


# ---------------------------------------------------------------------------
# Dispatcher functions — one per effect pattern
# ---------------------------------------------------------------------------

def _flat_mult(ctx: ScoreContext, j: dict, ability_key: str, fallback: float) -> None:
    ctx.mult += _ability(j).get(ability_key, fallback)

def _parsed_mult(ctx: ScoreContext, j: dict, fallback: float, **_kw) -> None:
    ctx.mult += _ab_mult(j, fallback=fallback)

def _parsed_chips(ctx: ScoreContext, j: dict, fallback: float, **_kw) -> None:
    ctx.chips += _ab_chips(j, fallback=fallback)

def _parsed_xmult(ctx: ScoreContext, j: dict, fallback: float, **_kw) -> None:
    ctx.mult *= _ab_xmult(j, fallback=fallback)

def _hand_mult(ctx: ScoreContext, j: dict, hands: list[str], ability_key: str, fallback: float) -> None:
    if _hand_contains(ctx, *hands):
        ctx.mult += _ability(j).get(ability_key, fallback)

def _hand_chips(ctx: ScoreContext, j: dict, hands: list[str], ability_key: str, fallback: float) -> None:
    if _hand_contains(ctx, *hands):
        ctx.chips += _ability(j).get(ability_key, fallback)

def _hand_xmult(ctx: ScoreContext, j: dict, hands: list[str], fallback: float, **_kw) -> None:
    if _hand_contains(ctx, *hands):
        ctx.mult *= _ab_xmult(j, fallback=fallback)

def _hand_parsed_chips(ctx: ScoreContext, j: dict, hands: list[str], fallback: float, **_kw) -> None:
    if _hand_contains(ctx, *hands):
        ctx.chips += _ab_chips(j, fallback=fallback)

def _suit_mult(ctx: ScoreContext, j: dict, suit: str, ability_key: str, fallback: float) -> None:
    ctx.mult += _ability(j).get(ability_key, fallback) * _count_suit_in_scoring(ctx, suit)


# ---------------------------------------------------------------------------
# Effect table — (joker_key, dispatcher, params)
# ---------------------------------------------------------------------------

_DISPATCHERS = {
    "flat_mult": _flat_mult,
    "parsed_mult": _parsed_mult,
    "parsed_chips": _parsed_chips,
    "parsed_xmult": _parsed_xmult,
    "hand_mult": _hand_mult,
    "hand_chips": _hand_chips,
    "hand_xmult": _hand_xmult,
    "hand_parsed_chips": _hand_parsed_chips,
    "suit_mult": _suit_mult,
}

SIMPLE_EFFECTS_TABLE: list[tuple[str, str, dict]] = [
    # Unconditional flat
    ("j_joker",       "flat_mult",   {"ability_key": "mult", "fallback": 4}),
    ("j_gros_michel", "flat_mult",   {"ability_key": "mult", "fallback": 15}),

    # Parsed (scaling/decay)
    ("j_popcorn",     "parsed_mult",  {"fallback": 12}),
    ("j_ice_cream",   "parsed_chips", {"fallback": 60}),
    ("j_cavendish",   "parsed_xmult", {"fallback": 3.0}),
    ("j_blue_joker",  "parsed_chips", {"fallback": 70}),

    # Hand-type +mult
    ("j_jolly",  "hand_mult",  {"hands": ["Pair"],            "ability_key": "t_mult", "fallback": 8}),
    ("j_zany",   "hand_mult",  {"hands": ["Three of a Kind"], "ability_key": "t_mult", "fallback": 12}),
    ("j_mad",    "hand_mult",  {"hands": ["Two Pair"],        "ability_key": "t_mult", "fallback": 10}),
    ("j_crazy",  "hand_mult",  {"hands": ["Straight"],        "ability_key": "t_mult", "fallback": 12}),
    ("j_droll",  "hand_mult",  {"hands": ["Flush"],           "ability_key": "t_mult", "fallback": 10}),

    # Hand-type +chips
    ("j_sly",     "hand_chips", {"hands": ["Pair"],            "ability_key": "t_chips", "fallback": 50}),
    ("j_wily",    "hand_chips", {"hands": ["Three of a Kind"], "ability_key": "t_chips", "fallback": 100}),
    ("j_clever",  "hand_chips", {"hands": ["Two Pair"],        "ability_key": "t_chips", "fallback": 80}),
    ("j_devious", "hand_chips", {"hands": ["Straight"],        "ability_key": "t_chips", "fallback": 100}),
    ("j_crafty",  "hand_chips", {"hands": ["Flush"],           "ability_key": "t_chips", "fallback": 80}),

    # Hand-type xmult
    ("j_duo",    "hand_xmult", {"hands": ["Pair"],            "fallback": 2.0}),
    ("j_trio",   "hand_xmult", {"hands": ["Three of a Kind"], "fallback": 3.0}),
    ("j_family", "hand_xmult", {"hands": ["Four of a Kind"],  "fallback": 4.0}),
    ("j_order",  "hand_xmult", {"hands": ["Straight"],        "fallback": 3.0}),
    ("j_tribe",  "hand_xmult", {"hands": ["Flush"],           "fallback": 2.0}),

    # Suit-conditional mult
    ("j_greedy_joker",    "suit_mult", {"suit": "D", "ability_key": "s_mult", "fallback": 3}),
    ("j_lusty_joker",     "suit_mult", {"suit": "H", "ability_key": "s_mult", "fallback": 3}),
    ("j_wrathful_joker",  "suit_mult", {"suit": "S", "ability_key": "s_mult", "fallback": 3}),
    ("j_gluttenous_joker","suit_mult", {"suit": "C", "ability_key": "s_mult", "fallback": 3}),

    # Simple scaling (parsed value)
    ("j_ceremonial",    "parsed_mult",  {"fallback": 10}),
    # j_ride_the_bus — moved to complex.py (needs face card check + pre-scoring increment)
    ("j_red_card",      "parsed_mult",  {"fallback": 6}),
    ("j_fortune_teller","parsed_mult",  {"fallback": 3}),
    ("j_flash",         "parsed_mult",  {"fallback": 6}),
    # j_trousers — moved to complex.py (needs pre-scoring increment on Two Pair)
    # j_runner — moved to complex.py (scores unconditionally + pre-scoring increment on Straight)
    # j_square — moved to complex.py (needs pre-scoring increment on 4-card plays)
    ("j_erosion",       "parsed_mult",  {"fallback": 8}),
    ("j_hiker",         "parsed_chips", {"fallback": 10}),
    ("j_stone",         "parsed_chips", {"fallback": 25}),
    ("j_castle",        "parsed_chips", {"fallback": 15}),
    ("j_constellation", "parsed_xmult", {"fallback": 1.3}),
    ("j_madness",       "parsed_xmult", {"fallback": 2.0}),
    ("j_vampire",       "parsed_xmult", {"fallback": 1.3}),
    ("j_hologram",      "parsed_xmult", {"fallback": 1.5}),
    ("j_obelisk",       "parsed_xmult", {"fallback": 1.6}),
    ("j_ramen",         "parsed_xmult", {"fallback": 1.5}),
    ("j_lucky_cat",     "parsed_xmult", {"fallback": 1.5}),
    ("j_campfire",      "parsed_xmult", {"fallback": 1.5}),
    ("j_hit_the_road",  "parsed_xmult", {"fallback": 1.5}),
    ("j_glass",         "parsed_xmult", {"fallback": 1.5}),
    ("j_throwback",     "parsed_xmult", {"fallback": 1.5}),
    ("j_canio",         "parsed_xmult", {"fallback": 2.0}),
    ("j_yorick",        "parsed_xmult", {"fallback": 2.0}),
    ("j_steel_joker",   "parsed_xmult", {"fallback": 1.2}),
]


def _build_simple_effects() -> dict[str, object]:
    """Build a dict of joker_key -> effect function from the table."""
    effects = {}
    for key, dispatch_name, params in SIMPLE_EFFECTS_TABLE:
        dispatcher = _DISPATCHERS[dispatch_name]
        # Create a closure that binds the dispatcher and params
        def make_effect(d=dispatcher, p=params):
            def effect(ctx: ScoreContext, j: dict) -> None:
                d(ctx, j, **p)
            return effect
        effects[key] = make_effect()
    return effects


SIMPLE_EFFECTS: dict[str, object] = _build_simple_effects()
