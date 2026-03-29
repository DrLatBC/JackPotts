"""Shared constants used across the balatro_bot package.

This module contains only literal data — no functions, no imports from other
balatro_bot modules. Everything can import from here without cycle risk.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Card data
# ---------------------------------------------------------------------------

RANK_ORDER: dict[str, int] = {
    "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8, "9": 9,
    "T": 10, "J": 11, "Q": 12, "K": 13, "A": 14,
}

RANK_CHIPS: dict[str, int] = {
    "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8, "9": 9,
    "T": 10, "J": 10, "Q": 10, "K": 10, "A": 11,
}

ALL_SUITS = {"H", "D", "C", "S"}

FACE_RANKS = {"J", "Q", "K"}
FIBONACCI_RANKS = {"A", "2", "3", "5", "8"}
EVEN_RANKS = {"T", "8", "6", "4", "2"}
ODD_RANKS = {"A", "9", "7", "5", "3"}

# ---------------------------------------------------------------------------
# Hand types
# ---------------------------------------------------------------------------

HAND_TYPES: list[tuple[str, int, int]] = [
    ("Flush Five",      160, 16),
    ("Flush House",     140, 14),
    ("Five of a Kind",  120, 12),
    ("Straight Flush",  100,  8),
    ("Four of a Kind",   60,  7),
    ("Full House",       40,  4),
    ("Flush",            35,  4),
    ("Straight",         30,  4),
    ("Three of a Kind",  30,  3),
    ("Two Pair",         20,  2),
    ("Pair",             10,  2),
    ("High Card",         5,  1),
]

HAND_INFO: dict[str, tuple[int, int, int]] = {
    name: (chips, mult, i)
    for i, (name, chips, mult) in enumerate(HAND_TYPES)
}

# ---------------------------------------------------------------------------
# Joker sets used by decision rules
# ---------------------------------------------------------------------------

SCALING_JOKERS = {
    "j_campfire", "j_constellation", "j_vampire", "j_hologram",
    "j_lucky_cat", "j_canio", "j_hit_the_road", "j_red_card", "j_green_joker",
}

FEWER_CARDS_JOKERS = {"j_half"}
ALL_SCORE_JOKERS = {"j_splash"}
EXACT_4_JOKERS = {"j_square"}

PLAY_SCALERS = {"j_green_joker", "j_supernova", "j_ride_the_bus", "j_trousers", "j_runner", "j_square"}
FINAL_HAND_JOKERS = {"j_acrobat", "j_dusk"}
DISCARD_SCALERS = {"j_castle", "j_hit_the_road"}
FACE_RANKS_SET = {"J", "Q", "K"}

# ---------------------------------------------------------------------------
# Consumable data
# ---------------------------------------------------------------------------

PLANET_KEYS: dict[str, str] = {
    "c_mercury": "Pair", "c_venus": "Three of a Kind", "c_earth": "Full House",
    "c_mars": "Four of a Kind", "c_jupiter": "Flush", "c_saturn": "Straight",
    "c_uranus": "Two Pair", "c_neptune": "Straight Flush", "c_pluto": "High Card",
    "c_planet_x": "Five of a Kind", "c_ceres": "Flush House", "c_eris": "Flush Five",
    "c_black_hole": "ALL",
}

# Planet card label -> hand type (for pack picking)
PLANET_HAND_MAP: dict[str, str] = {
    "Mercury": "Pair", "Venus": "Three of a Kind", "Earth": "Full House",
    "Mars": "Four of a Kind", "Jupiter": "Flush", "Saturn": "Straight",
    "Uranus": "Two Pair", "Neptune": "Straight Flush", "Pluto": "High Card",
    "Planet X": "Five of a Kind", "Ceres": "Flush House", "Eris": "Flush Five",
}

# No-target Tarots ranked by value (higher = better)
NO_TARGET_TAROTS: dict[str, int] = {
    "c_judgement": 6, "c_high_priestess": 5, "c_hermit": 4,
    "c_emperor": 3, "c_temperance": 2, "c_wheel_of_fortune": 1, "c_fool": 0,
}

# Tarots that need targets: key -> (max_targets, effect_type, extra)
TARGETING_TAROTS: dict[str, tuple] = {
    "c_lovers": (1, "enhance", "Wild"), "c_chariot": (1, "enhance", "Steel"),
    "c_hierophant": (2, "enhance", "Bonus"), "c_empress": (2, "enhance", "Mult"),
    "c_magician": (2, "enhance", "Lucky"), "c_devil": (1, "gold", None),
    "c_star": (3, "suit_convert", "D"), "c_moon": (3, "suit_convert", "C"),
    "c_sun": (3, "suit_convert", "H"), "c_world": (3, "suit_convert", "S"),
    "c_strength": (2, "rank_up", None), "c_justice": (1, "glass", None),
    "c_tower": (1, "stone", None), "c_hanged_man": (2, "destroy", None),
    "c_death": (2, "clone", None),
}

SAFE_CONSUMABLE_TAROTS = {
    "c_judgement", "c_high_priestess", "c_hermit", "c_emperor",
    "c_temperance", "c_wheel_of_fortune", "c_fool",
}

IMMEDIATE_TARGETING = {"destroy", "clone", "stone", "rank_up", "deck_enhance", "seal", "edition", "clone_deck"}
TACTICAL_TARGETING = {"suit_convert", "glass", "enhance", "gold"}

SAFE_SPECTRAL_CONSUMABLES = {
    "c_ankh", "c_immolate", "c_ectoplasm", "c_hex", "c_wraith",
}

SPECTRAL_TARGETING: dict[str, tuple] = {
    "c_familiar": (1, "deck_enhance", "face"),
    "c_grim": (1, "deck_enhance", "ace"),
    "c_incantation": (1, "deck_enhance", "number"),
    "c_talisman": (1, "seal", "Gold"),
    "c_deja_vu": (1, "seal", "Red"),
    "c_trance": (1, "seal", "Blue"),
    "c_medium": (1, "seal", "Purple"),
    "c_aura": (1, "edition", None),
    "c_cryptid": (1, "clone_deck", None),
}

FACE_RANKS_TAROT = {"J", "Q", "K"}
