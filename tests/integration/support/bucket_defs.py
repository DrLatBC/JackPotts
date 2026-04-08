"""Declarative bucket definitions for test_hook_scaling.py.

Each bucket defines a boss + joker combination to test scoring accuracy.

Schema:
    label       str          — Human-readable description
    boss        str          — Boss blind display name (must be in harness.BOSS_KEYS)
    jokers      list[str]    — Joker keys to inject
    track_keys  list[str]    — Joker keys to snapshot for diagnostics

Optional:
    ante        int          — Override ante (default: boss's minimum ante from BOSS_MIN_ANTE)
    no_god_mode     bool     — Skip inject_god_mode + inject_milk_trigger
    burn_discards   bool     — Burn 20 cards via discard in early rounds
"""

BUCKET_DEFS: dict[str, dict] = {
    "A": {
        "label": "Hook + decay (Ice Cream, Popcorn)",
        "boss": "The Hook",
        "jokers": ["j_ice_cream", "j_popcorn"],
        "track_keys": ["j_ice_cream", "j_popcorn", "j_green_joker"],
    },
    "A2": {
        "label": "Hook + Yorick/Ramen discard scaling",
        "boss": "The Hook",
        "jokers": ["j_yorick", "j_ramen"],
        "track_keys": ["j_yorick", "j_ramen", "j_green_joker"],
        "no_god_mode": True,
        "burn_discards": True,
    },
    "B": {
        "label": "Hook + state-dependent (Blue Joker, Half Joker)",
        "boss": "The Hook",
        "jokers": ["j_blue_joker", "j_half"],
        "track_keys": ["j_blue_joker", "j_half"],
    },
    "C": {
        "label": "Hook + scaling/conditional (Vampire, Loyalty Card)",
        "boss": "The Hook",
        "jokers": ["j_vampire", "j_loyalty_card"],
        "track_keys": ["j_vampire", "j_loyalty_card"],
    },
    "D": {
        "label": "Violet Vessel + money/state (Bull, Raised Fist, Supernova)",
        "boss": "Violet Vessel",
        "jokers": ["j_bull", "j_raised_fist", "j_supernova"],
        "track_keys": ["j_bull", "j_raised_fist", "j_supernova"],
    },
    "E": {
        "label": "Crimson Heart + scaling (Runner, Ramen, Joker Stencil)",
        "boss": "Crimson Heart",
        "jokers": ["j_runner", "j_ramen", "j_stencil"],
        "track_keys": ["j_runner", "j_ramen", "j_stencil"],
    },
}
