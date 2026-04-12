"""
Central scaling joker registry.

Every joker that permanently gains value gets a ScalingProfile describing
what triggers it, what it gains, and how the bot should exploit it.
All other modules import derived sets from here instead of maintaining
their own hardcoded lists.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ScalingProfile:
    """Describes a joker's scaling behavior for milking decisions."""

    key: str
    trigger: str
    """What action causes the scaling. Values:
    - play: any hand played
    - play_no_face: hand without face cards
    - play_4_cards: exactly 4 cards played
    - play_straight: hand containing a Straight
    - play_two_pair: hand containing Two Pair
    - play_non_favorite: hand that isn't the most-played type
    - play_enhanced: enhanced card scored
    - play_2: hand containing a 2
    - discard: any discard
    - discard_jack: discard a Jack
    - sell: sell a card in shop
    - planet: use a Planet consumable
    - skip_pack: skip a booster pack
    - reroll: reroll the shop
    - final_hand: last hand of the round
    - zero_discards: having 0 discards left
    - per_discard_left: scales with remaining discards (anti-discard)
    - passive: not directly triggerable by the bot
    """

    gain_type: str  # "mult", "chips", "xmult", "retrigger", "perma_chips"
    gain_per: float  # amount gained per trigger (0 for variable/passive)
    protect_from_sell: bool  # should SellWeakJoker protect this?
    milk_priority: int  # 0=don't actively milk, 1=low, 2=medium, 3=high
    notes: str = ""


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

SCALING_REGISTRY: dict[str, ScalingProfile] = {
    # --- Play scalers (gain value from playing hands) ---
    "j_green_joker": ScalingProfile(
        "j_green_joker", "play", "mult", 1, True, 3,
        "Also -1 per discard. Pure hand spammer.",
    ),
    "j_supernova": ScalingProfile(
        "j_supernova", "play", "mult", 1, False, 2,
        "+1 mult per time THIS hand type played this run.",
    ),
    "j_ride_the_bus": ScalingProfile(
        "j_ride_the_bus", "play_no_face", "mult", 1, False, 3,
        "Resets to 0 when face cards in hand. Avoid faces until kill shot.",
    ),
    "j_square": ScalingProfile(
        "j_square", "play_4_cards", "chips", 4, False, 2,
        "Every milk hand should be exactly 4 cards.",
    ),
    "j_runner": ScalingProfile(
        "j_runner", "play_straight", "chips", 15, True, 1,
        "Only scales on Straights. Chase Straight milk hands when possible.",
    ),
    "j_trousers": ScalingProfile(
        "j_trousers", "play_two_pair", "mult", 2, False, 1,
        "Only scales on Two Pair. Chase Two Pair milk hands when possible.",
    ),
    "j_obelisk": ScalingProfile(
        "j_obelisk", "play_non_favorite", "xmult", 0.2, True, 1,
        "Gains when you play a hand that ISN'T the most-played type. Resets otherwise.",
    ),
    "j_hiker": ScalingProfile(
        "j_hiker", "play", "perma_chips", 5, False, 2,
        "+5 permanent chips on every played card. Every hand makes cards beefier.",
    ),
    "j_wee": ScalingProfile(
        "j_wee", "play_2", "chips", 8, False, 1,
        "Only scales when a 2 is scored. Include 2s in milk hands.",
    ),
    "j_vampire": ScalingProfile(
        "j_vampire", "play_enhanced", "xmult", 0.1, True, 1,
        "Strips enhancement but gains xmult. Play enhanced cards to feed it.",
    ),

    # --- Discard scalers ---
    "j_castle": ScalingProfile(
        "j_castle", "discard", "chips", 3, True, 2,
        "+3 chips per discarded card of the current suit.",
    ),
    "j_hit_the_road": ScalingProfile(
        "j_hit_the_road", "discard_jack", "xmult", 0.5, True, 3,
        "Dump Jacks early. Resets each round.",
    ),
    "j_yorick": ScalingProfile(
        "j_yorick", "discard", "xmult", 1, True, 2,
        "+1 xmult per 23 cards discarded total. Burn discards aggressively.",
    ),

    # --- Shop scalers ---
    "j_campfire": ScalingProfile(
        "j_campfire", "sell", "xmult", 0.25, True, 3,
        "Buy cheap stuff to sell. Resets at boss blind.",
    ),
    "j_constellation": ScalingProfile(
        "j_constellation", "planet", "xmult", 0.1, True, 3,
        "Every planet is permanent xmult. Buy ALL planets, even off-strategy.",
    ),
    "j_red_card": ScalingProfile(
        "j_red_card", "skip_pack", "mult", 3, True, 2,
        "Skip packs you don't need.",
    ),
    "j_flash": ScalingProfile(
        "j_flash", "reroll", "mult", 2, False, 1,
        "Rerolling is +2 mult investment, not waste.",
    ),

    # --- Passive scalers (not directly triggerable) ---
    "j_hologram": ScalingProfile(
        "j_hologram", "passive", "xmult", 0.25, True, 0,
        "Scales when cards added to deck (DNA, Marble, Spectral).",
    ),
    "j_lucky_cat": ScalingProfile(
        "j_lucky_cat", "passive", "xmult", 0.25, True, 0,
        "Scales on Lucky card triggers. Stack Lucky enhancements.",
    ),
    "j_canio": ScalingProfile(
        "j_canio", "passive", "xmult", 1, True, 0,
        "Scales when face cards destroyed (Glass, Spectral). Not directly triggerable.",
    ),
    "j_madness": ScalingProfile(
        "j_madness", "passive", "xmult", 0.5, True, 0,
        "+X0.5 per blind select. Destroys a random joker. Run-defining.",
    ),
    "j_ceremonial": ScalingProfile(
        "j_ceremonial", "passive", "mult", 0, True, 0,
        "Absorbs sell value of right joker. Park fodder to the right.",
    ),

    # --- Final hand / conditional (affect HOW to milk, not what to milk) ---
    "j_acrobat": ScalingProfile(
        "j_acrobat", "final_hand", "xmult", 3, False, 2,
        "X3 on last hand. Always save the winning hand for last.",
    ),
    "j_dusk": ScalingProfile(
        "j_dusk", "final_hand", "retrigger", 2, False, 2,
        "Retrigger all cards on last hand. Save winning hand for last.",
    ),
    "j_mystic_summit": ScalingProfile(
        "j_mystic_summit", "zero_discards", "mult", 15, False, 1,
        "+15 mult when 0 discards left. Burn all discards before winning.",
    ),
    "j_banner": ScalingProfile(
        "j_banner", "per_discard_left", "chips", 30, False, 1,
        "+30 chips per remaining discard. DON'T burn discards.",
    ),

    # --- Decay jokers (anti-milk, avoid extra actions) ---
    "j_ice_cream": ScalingProfile(
        "j_ice_cream", "play", "chips", -5, False, 0,
        "LOSES 5 chips per hand. Milking costs real score. Sell before 0.",
    ),
    "j_ramen": ScalingProfile(
        "j_ramen", "discard", "xmult", -0.01, False, 0,
        "LOSES 0.01 xmult per card discarded. Don't discard when owned.",
    ),
    "j_popcorn": ScalingProfile(
        "j_popcorn", "passive", "mult", -4, False, 0,
        "LOSES 4 mult per round. Sell before it decays to 0.",
    ),
}


# ---------------------------------------------------------------------------
# Derived sets — used by other modules
# ---------------------------------------------------------------------------

def _keys_where(pred) -> frozenset[str]:
    return frozenset(k for k, p in SCALING_REGISTRY.items() if pred(p))


# Jokers that scale from playing hands (any trigger starting with "play")
PLAY_SCALERS = _keys_where(lambda p: p.trigger.startswith("play") and p.milk_priority > 0)

# Jokers that scale from discarding
DISCARD_SCALERS = _keys_where(lambda p: p.trigger.startswith("discard") and p.milk_priority > 0)

# Jokers that want the winning hand played LAST
FINAL_HAND_JOKERS = _keys_where(lambda p: p.trigger == "final_hand")

# Jokers that scale from shop actions
SHOP_SCALERS = _keys_where(lambda p: p.trigger in ("sell", "planet", "skip_pack", "reroll"))

# Jokers that should never be sold
SELL_PROTECTED = _keys_where(lambda p: p.protect_from_sell)

# All jokers with any scaling behavior (for gates, logging, etc.)
ALL_SCALING = frozenset(SCALING_REGISTRY.keys())

# xMult scalers — compound multiplicatively, still valuable late game
SCALING_XMULT = _keys_where(lambda p: p.gain_type == "xmult" and p.gain_per > 0)

# Jokers that DECAY (anti-milk) — other rules check these to avoid milking
DECAY_JOKERS = _keys_where(lambda p: p.gain_per < 0)

# Anti-discard jokers: owned → don't burn discards
ANTI_DISCARD = _keys_where(
    lambda p: p.trigger == "per_discard_left" or (p.trigger == "discard" and p.gain_per < 0)
) | {"j_green_joker"}  # Green Joker loses mult on discard


# ---------------------------------------------------------------------------
# Anti-synergy map: joker pairs that directly conflict.
# Don't buy/pick a joker when an owned joker is in its conflict set.
# ---------------------------------------------------------------------------

_DISCARD_SCALERS_SET = {"j_castle", "j_yorick", "j_hit_the_road"}
_FACE_JOKERS_SET = {"j_photograph", "j_scary_face", "j_smiley", "j_triboulet"}
_PLAY_SCALERS_SET = {"j_green_joker", "j_supernova", "j_ride_the_bus"}

ANTI_SYNERGY: dict[str, frozenset[str]] = {
    # Discard resource conflicts
    "j_banner":         frozenset({"j_mystic_summit"} | _DISCARD_SCALERS_SET),
    "j_mystic_summit":  frozenset({"j_banner"}),
    "j_ramen":          frozenset(_DISCARD_SCALERS_SET),
    "j_delayed_grat":   frozenset(_DISCARD_SCALERS_SET | {"j_mystic_summit"}),
    "j_burglar":        frozenset({"j_banner"}),
    # Play-count conflicts (decay vs scaling)
    "j_ice_cream":      frozenset(_PLAY_SCALERS_SET),
    # Card count conflicts
    "j_half":           frozenset({"j_square"}),
    "j_square":         frozenset({"j_half"}),
    # Face card conflicts
    "j_ride_the_bus":   frozenset(_FACE_JOKERS_SET),
    # Destruction conflicts
    "j_ceremonial":     frozenset({"j_madness"}),
    # Slot conflicts
    "j_stencil":        frozenset({"j_abstract", "j_riff_raff"}),
    "j_abstract":       frozenset({"j_stencil"}),
    "j_riff_raff":      frozenset({"j_stencil"}),
    # Enhancement conflicts
    "j_vampire":        frozenset({"j_glass", "j_lucky_cat"}),
    # Strategy conflicts
    "j_obelisk":        frozenset({"j_supernova"}),
    "j_supernova":      frozenset({"j_obelisk"}),
}


def check_anti_synergy(candidate_key: str, owned_keys: set[str]) -> str | None:
    """Return the name of a conflicting owned joker, or None if no conflict."""
    conflicts = ANTI_SYNERGY.get(candidate_key, frozenset())
    hit = conflicts & owned_keys
    return sorted(hit)[0] if hit else None
