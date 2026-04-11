"""Unified joker valuation system.

Single entry point ``evaluate_joker_value()`` produces a comparable float for
any joker, used by both BuyJokersInShop and SellWeakJoker.

Three layers:
  1. Scoring line simulation — score with/without candidate across preferred hands
  2. Synergy multiplier — amplification pairs, trigger coherence, archetype bonus
  3. Context scaling — ante urgency, diminishing returns by category
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from balatro_bot.cards import joker_key
from balatro_bot.domain.models.card import Card, CardValue
from balatro_bot.domain.scoring.estimate import score_hand
from balatro_bot.joker_effects import JOKER_EFFECTS, _noop, parse_effect_value
from balatro_bot.strategy import (
    ARCHETYPE_REGISTRY,
    JOKER_HAND_AFFINITY,
    Strategy,
    compute_strategy,
)

if TYPE_CHECKING:
    pass

# ---------------------------------------------------------------------------
# Scoring category metadata (moved from BuyJokersInShop)
# ---------------------------------------------------------------------------

JOKER_SCORE_CATEGORY: dict[str, set[str]] = {
    "xmult": {
        "j_cavendish", "j_stencil",
        "j_duo", "j_trio", "j_family", "j_order", "j_tribe",
        "j_photograph", "j_baron", "j_bloodstone", "j_triboulet",
        "j_blackboard", "j_acrobat", "j_flower_pot", "j_seeing_double",
        "j_steel_joker", "j_loyalty_card", "j_drivers_license",
        "j_madness", "j_vampire", "j_hologram", "j_obelisk",
        "j_lucky_cat", "j_glass", "j_campfire", "j_throwback",
        "j_card_sharp", "j_ancient", "j_baseball", "j_canio",
        "j_yorick", "j_hit_the_road", "j_constellation", "j_idol",
    },
    "mult": {
        "j_joker", "j_misprint", "j_gros_michel", "j_popcorn",
        "j_jolly", "j_zany", "j_mad", "j_crazy", "j_droll",
        "j_greedy_joker", "j_lusty_joker", "j_wrathful_joker", "j_gluttenous_joker",
        "j_onyx_agate",
        "j_smiley", "j_fibonacci", "j_even_steven",
        "j_shoot_the_moon", "j_raised_fist",
        "j_half", "j_abstract", "j_mystic_summit", "j_bootstraps",
        "j_swashbuckler", "j_erosion",
        "j_ceremonial", "j_supernova", "j_ride_the_bus", "j_green_joker",
        "j_red_card", "j_flash", "j_fortune_teller", "j_trousers", "j_ramen",
    },
    "chips": {
        "j_blue_joker", "j_stuntman", "j_ice_cream",
        "j_sly", "j_wily", "j_clever", "j_devious", "j_crafty",
        "j_arrowhead",
        "j_scary_face", "j_odd_todd",
        "j_banner", "j_bull",
        "j_runner", "j_square", "j_castle", "j_wee", "j_hiker", "j_stone",
    },
}

# Reverse lookup: joker key -> category
_KEY_TO_CATEGORY: dict[str, str] = {}
for _cat, _keys in JOKER_SCORE_CATEGORY.items():
    for _k in _keys:
        _KEY_TO_CATEGORY[_k] = _cat


# ---------------------------------------------------------------------------
# Utility joker base values (moved from shop.py)
# ---------------------------------------------------------------------------

UTILITY_VALUE: dict[str, float] = {
    "j_chicot":        4.0,
    "j_mr_bones":      3.5,
    "j_perkeo":        3.0,
    "j_four_fingers":  2.5,
    "j_smeared":       2.5,
    "j_shortcut":      2.0,
    "j_splash":        2.0,
    "j_pareidolia":    2.0,
    "j_cartomancer":   2.0,
    "j_hallucination": 1.5,
    "j_space":         1.5,
    "j_8_ball":        1.5,
    "j_sixth_sense":   1.0,
    "j_seance":        1.0,
    "j_superposition": 1.0,
    "j_riff_raff":     1.0,
    "j_oops":          1.5,
    "j_merry_andy":    1.5,
    "j_turtle_bean":   1.5,
    "j_drunkard":      1.0,
    "j_burnt":         1.0,
    "j_juggler":       1.0,
    "j_troubadour":    1.0,
    "j_vagabond":      0.5,
    "j_marble":        0.5,
    "j_dna":           0.5,
    "j_certificate":   0.5,
    "j_midas_mask":    0.5,
    "j_hack":          1.5,
    "j_hanging_chad":  1.0,
    "j_dusk":          1.5,
    "j_sock_and_buskin": 1.0,
    "j_selzer":       1.5,
    "j_mime":          1.0,
    "j_luchador":      1.0,
    "j_invisible":     1.0,
    "j_diet_cola":     0.5,
    "j_burglar":       0.5,
    "j_ring_master":   0.5,
}


# ---------------------------------------------------------------------------
# Synthetic hand builder
# ---------------------------------------------------------------------------

_DEFAULT_SUIT = "H"
_TYPICAL_RANK = "7"  # ~average chip value
_FILLER_RANKS = ["3", "4", "5", "6", "9"]  # ranks unlikely to form pairs
_STRAIGHT_RANKS = ["5", "6", "7", "8", "9"]


def _make_card(rank: str, suit: str) -> Card:
    """Build a minimal synthetic Card for scoring simulation."""
    return Card(
        id=0,
        key=f"{suit}_{rank}",
        set_="DEFAULT",
        label=f"{rank} of {suit}",
        value=CardValue(rank=rank, suit=suit),
    )


def _preferred_suit(strategy: Strategy | None) -> str:
    if strategy and strategy.preferred_suits:
        return strategy.preferred_suits[0][0]
    return _DEFAULT_SUIT


def _preferred_rank(strategy: Strategy | None) -> str:
    if strategy and strategy.preferred_ranks:
        return strategy.preferred_ranks[0][0]
    return _TYPICAL_RANK


def _alt_suit(primary: str) -> str:
    """Return a suit different from primary."""
    return "D" if primary != "D" else "C"


def _synthetic_hand(
    hand_name: str,
    strategy: Strategy | None = None,
) -> tuple[list[dict], list[dict]]:
    """Build (scoring_cards, played_cards) for a typical hand of given type.

    scoring_cards: subset that actually scores in Balatro
    played_cards: all 5 cards played
    """
    suit = _preferred_suit(strategy)
    rank = _preferred_rank(strategy)
    alt = _alt_suit(suit)

    if hand_name == "High Card":
        scoring = [_make_card("A", suit)]
        filler = [_make_card(r, alt) for r in _FILLER_RANKS[:4]]
        return scoring, scoring + filler

    if hand_name == "Pair":
        scoring = [_make_card(rank, suit), _make_card(rank, alt)]
        filler = [_make_card(r, alt) for r in _FILLER_RANKS[:3]]
        return scoring, scoring + filler

    if hand_name == "Two Pair":
        r2 = "T" if rank != "T" else "J"
        scoring = [
            _make_card(rank, suit), _make_card(rank, alt),
            _make_card(r2, suit), _make_card(r2, alt),
        ]
        filler = [_make_card(_FILLER_RANKS[0], alt)]
        return scoring, scoring + filler

    if hand_name == "Three of a Kind":
        s2 = "D" if suit not in ("D",) else "C"
        scoring = [
            _make_card(rank, suit),
            _make_card(rank, alt),
            _make_card(rank, s2),
        ]
        filler = [_make_card(r, alt) for r in _FILLER_RANKS[:2]]
        return scoring, scoring + filler

    if hand_name == "Straight":
        scoring = [_make_card(r, suit if i % 2 == 0 else alt)
                   for i, r in enumerate(_STRAIGHT_RANKS)]
        return scoring, list(scoring)

    if hand_name == "Flush":
        ranks = [rank] + [r for r in _FILLER_RANKS if r != rank][:4]
        scoring = [_make_card(r, suit) for r in ranks]
        return scoring, list(scoring)

    if hand_name == "Full House":
        r2 = "T" if rank != "T" else "J"
        s2 = "D" if suit not in ("D",) else "C"
        scoring = [
            _make_card(rank, suit), _make_card(rank, alt), _make_card(rank, s2),
            _make_card(r2, suit), _make_card(r2, alt),
        ]
        return scoring, list(scoring)

    if hand_name == "Four of a Kind":
        scoring = [
            _make_card(rank, "H"), _make_card(rank, "D"),
            _make_card(rank, "C"), _make_card(rank, "S"),
        ]
        filler = [_make_card(_FILLER_RANKS[0], _DEFAULT_SUIT)]
        return scoring, scoring + filler

    if hand_name == "Straight Flush":
        scoring = [_make_card(r, suit) for r in _STRAIGHT_RANKS]
        return scoring, list(scoring)

    if hand_name in ("Flush Five", "Five of a Kind"):
        scoring = [_make_card(rank, suit)] * 5
        return scoring, list(scoring)

    if hand_name == "Flush House":
        r2 = "T" if rank != "T" else "J"
        scoring = [
            _make_card(rank, suit), _make_card(rank, suit), _make_card(rank, suit),
            _make_card(r2, suit), _make_card(r2, suit),
        ]
        return scoring, list(scoring)

    # Fallback: treat as High Card
    scoring = [_make_card("A", suit)]
    filler = [_make_card(r, alt) for r in _FILLER_RANKS[:4]]
    return scoring, scoring + filler


# ---------------------------------------------------------------------------
# Layer 1: Scoring line simulation
# ---------------------------------------------------------------------------

def _dynamic_power(parsed: dict) -> float:
    """Convert parsed effect values to a power score (same scale as old system)."""
    power = 0.0
    xm = parsed.get("xmult")
    if xm and xm > 1.0:
        power = xm * 5.0  # X2 → 10.0, X3 → 15.0
    m = parsed.get("mult")
    if m and m > 0:
        power = max(power, m / 5.0)  # +30 → 6.0
    c = parsed.get("chips")
    if c and c > 0:
        power = max(power, c / 50.0)  # +100 → 2.0
    return power


def _has_scoring_effect(key: str) -> bool:
    effect = JOKER_EFFECTS.get(key)
    return effect is not None and effect is not _noop


def _scoring_delta(
    candidate: dict,
    owned_jokers: list[dict],
    hand_levels: dict[str, dict],
    hand_types: list[tuple[str, float]],
    joker_limit: int = 5,
    strategy: Strategy | None = None,
) -> float:
    """Score with/without candidate across weighted hand types.

    Returns a weighted fractional improvement (0.5 = 50% average improvement).
    Filters the candidate out of owned_jokers so sell evaluations measure the
    true marginal value (not the value of a duplicate).
    """
    total_weight = sum(w for _, w in hand_types)
    if total_weight <= 0:
        return 0.0

    # Filter candidate from owned to handle sell evaluations correctly
    candidate_key = candidate.get("key", "")
    baseline_jokers = [j for j in owned_jokers if j is not candidate]

    weighted_delta = 0.0

    for hand_name, weight in hand_types:
        scoring_cards, played_cards = _synthetic_hand(hand_name, strategy)

        baseline = score_hand(
            hand_name, scoring_cards, hand_levels,
            jokers=baseline_jokers, played_cards=played_cards,
            joker_limit=joker_limit,
        )
        with_candidate = score_hand(
            hand_name, scoring_cards, hand_levels,
            jokers=baseline_jokers + [candidate], played_cards=played_cards,
            joker_limit=joker_limit,
        )

        base_total = max(baseline[2], 1)
        delta = (with_candidate[2] - baseline[2]) / base_total
        weighted_delta += delta * (weight / total_weight)

    return weighted_delta


# ---------------------------------------------------------------------------
# Layer 2: Synergy multiplier
# ---------------------------------------------------------------------------

# Data-driven amplification pairs: (enabler_key, boosted_keys, multiplier)
# Bidirectional: if candidate is enabler and owned has boosted (or vice versa)
_AMPLIFICATION_PAIRS: list[tuple[str, frozenset[str], float]] = [
    ("j_pareidolia", frozenset({
        "j_photograph", "j_scary_face", "j_smiley",
        "j_triboulet", "j_sock_and_buskin",
    }), 2.5),
    ("j_smeared", frozenset({
        "j_greedy_joker", "j_lusty_joker", "j_wrathful_joker",
        "j_gluttenous_joker", "j_arrowhead", "j_onyx_agate",
        "j_bloodstone", "j_rough_gem",
        "j_tribe", "j_droll", "j_crafty",
    }), 1.5),
    ("j_four_fingers", frozenset({
        "j_order", "j_tribe", "j_crazy", "j_droll",
        "j_crafty", "j_devious",
    }), 1.5),
    ("j_shortcut", frozenset({
        "j_order", "j_crazy", "j_devious", "j_runner",
    }), 1.5),
    ("j_splash", frozenset({
        "j_hiker", "j_fibonacci", "j_hack",
        "j_even_steven", "j_odd_todd",
    }), 1.5),
    ("j_oops", frozenset({
        "j_bloodstone", "j_lucky_cat", "j_8_ball", "j_space",
    }), 1.5),
    ("j_ride_the_bus", frozenset({
        "j_even_steven", "j_odd_todd", "j_hack",
        "j_fibonacci", "j_wee",
    }), 1.3),
]

# Blueprint/Brainstorm copy targets
_COPY_JOKERS = frozenset({"j_blueprint", "j_brainstorm"})
_XMULT_COPY_TARGETS = frozenset({
    "j_cavendish", "j_stencil", "j_duo", "j_trio", "j_family",
    "j_order", "j_tribe", "j_acrobat", "j_blackboard", "j_flower_pot",
    "j_madness", "j_vampire", "j_hologram", "j_constellation",
    "j_campfire", "j_lucky_cat", "j_canio", "j_obelisk",
    "j_card_sharp", "j_seeing_double",
})


def _utility_synergy_bonus(key: str, owned_keys: set[str], strat: Strategy) -> float:
    """Additive bonus for utility jokers based on build synergy."""
    bonus = 0.0

    if key == "j_four_fingers":
        if strat.hand_affinity("Flush") > 0 or strat.hand_affinity("Straight") > 0:
            bonus += 2.0
    elif key == "j_smeared" and strat.hand_affinity("Flush") > 0:
        bonus += 3.0
    elif key == "j_shortcut" and strat.hand_affinity("Straight") > 0:
        bonus += 2.0
    elif key == "j_pareidolia":
        face_jokers = {"j_photograph", "j_scary_face", "j_smiley",
                       "j_triboulet", "j_sock_and_buskin"}
        bonus += len(owned_keys & face_jokers) * 2.0
    elif key == "j_8_ball" and strat.rank_affinity("8") > 0:
        bonus += 2.0
    elif key == "j_oops":
        prob_jokers = {"j_8_ball", "j_space", "j_sixth_sense",
                       "j_bloodstone", "j_lucky_cat"}
        bonus += len(owned_keys & prob_jokers) * 1.5
    elif key == "j_space" and strat.top_hand():
        bonus += 1.0
    elif key == "j_merry_andy":
        discard_jokers = {"j_castle", "j_yorick", "j_hit_the_road"}
        bonus += len(owned_keys & discard_jokers) * 2.0
    elif key == "j_marble" and "j_stone" in owned_keys:
        bonus += 2.0
    elif key == "j_splash":
        per_card = {"j_hiker", "j_selzer", "j_hanging_chad"}
        bonus += len(owned_keys & per_card) * 1.5

    return bonus


def _synergy_multiplier(
    candidate_key: str,
    owned_keys: set[str],
    strategy: Strategy,
    owned_jokers: list[dict],
) -> float:
    """Unified synergy multiplier replacing _cross_synergy + coherence bonus."""
    mult = 1.0

    # --- Amplification pairs (data-driven) ---
    for enabler, boosted, factor in _AMPLIFICATION_PAIRS:
        # Candidate is boosted, enabler is owned
        if candidate_key in boosted and enabler in owned_keys:
            mult *= factor
        # Candidate is the enabler, owned has boosted jokers
        if candidate_key == enabler and owned_keys & boosted:
            mult *= factor

    # Blueprint/Brainstorm copy synergy
    if owned_keys & _COPY_JOKERS and candidate_key in _XMULT_COPY_TARGETS:
        mult *= 1.3
    if candidate_key in _COPY_JOKERS:
        if owned_keys & _XMULT_COPY_TARGETS:
            mult *= 1.3

    # --- Trigger coherence (hand type overlap) ---
    candidate_hands = set(JOKER_HAND_AFFINITY.get(candidate_key, ([], 0))[0])
    if candidate_hands:
        allies = 0
        for j in owned_jokers:
            okey = joker_key(j)
            if okey == candidate_key or okey not in JOKER_HAND_AFFINITY:
                continue
            other_hands = set(JOKER_HAND_AFFINITY[okey][0])
            if candidate_hands & other_hands:
                allies += 1
        mult *= 1.0 + allies * 0.15

    # --- Archetype coherence ---
    for arch_name, arch_strength in strategy.active_archetypes:
        profile = ARCHETYPE_REGISTRY.get(arch_name)
        if profile and (candidate_key in profile.joker_weights or candidate_key in profile.amplifiers):
            mult *= 1.0 + arch_strength * 0.15

    return mult


# ---------------------------------------------------------------------------
# Layer 3: Context scaling
# ---------------------------------------------------------------------------

def _context_scale(
    candidate_key: str,
    owned_jokers: list[dict],
    ante: int,
) -> float:
    """Context-dependent scaling: ante urgency + diminishing returns."""
    cat = _KEY_TO_CATEGORY.get(candidate_key)
    factor = 1.0

    # Ante urgency: xMult critical late, flat less so
    if cat and ante >= 4:
        if cat == "xmult":
            factor *= min(1.0 + (ante - 3) * 0.4, 2.6)   # 1.4 @ ante 4, 2.6 @ ante 7+
        else:
            factor *= max(1.0 - (ante - 3) * 0.1, 0.6)    # 0.9 @ ante 4, 0.6 @ ante 7+

    # Diminishing returns within same category
    if cat:
        same_count = sum(
            1 for j in owned_jokers
            if _KEY_TO_CATEGORY.get(joker_key(j)) == cat
        )
        factor *= 1.0 / (1.0 + same_count * 0.25)

    return factor


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def evaluate_joker_value(
    candidate: dict,
    owned_jokers: list[dict],
    hand_levels: dict[str, dict],
    ante: int,
    strategy: Strategy | None = None,
    joker_limit: int = 5,
) -> float:
    """Unified joker valuation. Returns ~0.0 to ~15.0.

    Higher = more valuable to the current build.
    Used by both BuyJokersInShop and SellWeakJoker.
    """
    key = candidate.get("key", "")
    if strategy is None:
        strategy = compute_strategy(owned_jokers, hand_levels)

    owned_keys = {joker_key(j) for j in owned_jokers}

    # Determine hand types to simulate
    if strategy.preferred_hands:
        hand_types = strategy.preferred_hands[:3]
    else:
        hand_types = [("Pair", 1.0), ("High Card", 0.5)]

    # Layer 1: scoring simulation or utility fallback
    if _has_scoring_effect(key):
        raw_delta = _scoring_delta(candidate, owned_jokers, hand_levels, hand_types, joker_limit, strategy)
        base_value = math.log2(1.0 + max(raw_delta, 0.0)) * 3.0
    else:
        base_value = UTILITY_VALUE.get(key, 0.0)
        base_value += _utility_synergy_bonus(key, owned_keys, strategy)

    # For jokers with parsed accumulated values (scaling jokers), take the
    # max of simulation and dynamic power from effect text
    effect_text = candidate.get("value", {}).get("effect", "")
    if effect_text:
        parsed = parse_effect_value(effect_text)
        dp = _dynamic_power(parsed)
        base_value = max(base_value, dp)

    # Layer 2: synergy
    synergy = _synergy_multiplier(key, owned_keys, strategy, owned_jokers)

    # Layer 3: context
    context = _context_scale(key, owned_jokers, ante)

    return base_value * synergy * context + _edition_bonus(candidate)


def _edition_bonus(card: dict) -> float:
    """Additive value bonus for joker editions.

    Polychrome (×1.5 every hand) is massive.  Holo (+10 mult) is solid.
    Foil (+50 chips) is minor.  Negative is handled separately in shop.py.
    """
    mod = card.get("modifier")
    if not isinstance(mod, dict):
        return 0.0
    edition = mod.get("edition")
    if edition == "POLYCHROME":
        return 4.0
    if edition in ("HOLO", "HOLOGRAPHIC"):
        return 1.5
    if edition == "FOIL":
        return 0.5
    return 0.0
