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
import os
import random
import zlib
from typing import TYPE_CHECKING

from balatro_bot.cards import joker_key
from balatro_bot.domain.models.card import Card, CardModifier, CardValue
from balatro_bot.domain.policy import utility_value
from balatro_bot.domain.policy.scaling_projection import (
    SCALING_XMULT_KEYS,
    project_additive_total,
    project_total_xmult,
)
from balatro_bot.domain.policy.boss_adjustment import (
    boss_multiplier, shop_blended_multiplier,
)
from balatro_bot.domain.policy.sim_context import SimContext
from balatro_bot.domain.scoring.estimate import score_hand
from balatro_bot.joker_effects import JOKER_EFFECTS, _noop
from balatro_bot.joker_effects.parsers import _get_parsed_value
from balatro_bot.joker_effects.scoring_phase import (
    PHASE_MULT, PHASE_XMULT, get_joker_phase, reorder_for_scoring,
)
from balatro_bot.scaling import (
    BLUEPRINT_INCOMPATIBLE, CONDITIONAL_XMULT, SCALING_REGISTRY,
)
from balatro_bot.strategy import (
    ARCHETYPE_REGISTRY,
    JOKER_HAND_AFFINITY,
    JOKER_RANK_AFFINITY,
    JOKER_SUIT_AFFINITY,
    Strategy,
    compute_strategy,
)

if TYPE_CHECKING:
    from balatro_bot.domain.models.deck_profile import DeckProfile
    from balatro_bot.domain.policy.sim_context import LiveRunStats

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
        "j_card_sharp", "j_ancient", "j_baseball", "j_caino",
        "j_yorick", "j_hit_the_road", "j_constellation", "j_idol",
    },
    "mult": {
        "j_joker", "j_misprint", "j_gros_michel", "j_popcorn",
        "j_jolly", "j_zany", "j_mad", "j_crazy", "j_droll",
        "j_greedy_joker", "j_lusty_joker", "j_wrathful_joker", "j_gluttenous_joker",
        "j_onyx_agate",
        "j_smiley", "j_fibonacci", "j_even_steven",
        "j_shoot_the_moon", "j_raised_fist", "j_mime",
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
# Layer 1 tuning — scoring simulation → base_value
#
# These are the dials that convert ``_scoring_delta`` (fractional improvement
# from the synthetic-hand sim) into a base score. Split per category so we can
# tune xmult independently of flat mult / chips without uniform sweeps.
# See also: ``_context_scale`` for Layer 3 ante urgency (separate domain).
# ---------------------------------------------------------------------------

# Compression coefficient on ``log2(1 + raw_delta)``. Higher = aligned strong
# jokers score bigger. Xmult uses a higher coefficient because its deltas are
# genuinely larger; flat mult/chips stay at the historical 3.0.
SIM_COEFF_XMULT = 5.0
SIM_COEFF_MULT = 3.0
SIM_COEFF_CHIPS = 3.0
SIM_COEFF_DEFAULT = 3.0  # uncategorized jokers

# Pivot-possible floor for hand-conditional Cat 1 xmult jokers (Duo/Trio/
# Tribe/Family/Order) when the roster hasn't committed to their trigger hand.
# Decays across antes: by mid-game there's no runway to pivot into their hand.
PIVOT_FLOOR_A1 = 2.5        # floor at ante 1
PIVOT_FLOOR_DECAY = 0.5     # subtract this per ante (hits 0 at ante 6)
PIVOT_FLOOR_THRESHOLD = 2.5  # sim base_value must be below this to trigger floor

# Phase 9: deck-sampled Monte Carlo. Every scoring valuation now samples hands
# from the actual deck profile (rank/suit/enhancement counts) rather than
# fabricating a hand engineered to make the candidate proc. This fixes the
# chronic overvaluation of rank-conditional flat-mult jokers (Scholar, Walkie
# Talkie) that die at ante 4 when their target rank shows up once every 3 hands
# instead of once per hand.
#
# Opt-out via ``BALATRO_MC_VALUATION=0`` for A/B comparison in shadow batches.
_MC_SAMPLING_ENABLED = os.environ.get("BALATRO_MC_VALUATION", "1") != "0"

# Jokers whose sim delta is invariant across hand composition: their
# contribution is either a global xmult/mult (Hologram, Stencil) or is driven
# entirely by a runway projection floor that bypasses the sim (anything in
# SCALING_XMULT_KEYS — see the ``max(base_value, projected_floor)`` gate).
# Use N=1 for these. Every other candidate takes the full N=16 pass.
_CARD_AGNOSTIC: frozenset[str] = SCALING_XMULT_KEYS | frozenset({
    "j_joker",         # +4 mult flat
    "j_stencil",       # xmult per empty joker slot
    "j_swashbuckler",  # mult per joker sell value
    "j_erosion",       # mult per missing deck card
    "j_flash",         # mult per reroll this run
    "j_red_card",      # mult per pack skipped
    "j_ride_the_bus",  # mult per consecutive no-face hand (run-history)
    "j_supernova",     # mult per hand played
    "j_obelisk",       # xmult per non-repeat hand
})

# Default sample counts.
_MC_FULL_SAMPLES = 32
_MC_AGNOSTIC_SAMPLES = 1

# Stochastic jokers still bump the per-sample rng so score_hand's probability
# branches (Misprint, Lucky, Bloodstone) cancel across CRN pairs. Sampling
# itself is always on when _MC_SAMPLING_ENABLED.
_STOCHASTIC_KEYS: frozenset[str] = frozenset({
    "j_misprint", "j_bloodstone", "j_lucky_cat", "j_oops",
})

# Kept for transitional fallback when _MC_SAMPLING_ENABLED is off.
_MC_DEFAULT_SAMPLES = 16

# ---------------------------------------------------------------------------
# Utility joker base values (moved from shop.py)
# ---------------------------------------------------------------------------

UTILITY_VALUE: dict[str, float] = {
    "j_chicot":        4.0,
    "j_mr_bones":      3.5,
    "j_four_fingers":  2.5,
    "j_smeared":       2.5,
    "j_shortcut":      2.0,
    "j_splash":        2.0,
    "j_pareidolia":    2.0,
    "j_superposition": 1.0,
    "j_riff_raff":     1.0,
    "j_oops":          1.5,
    "j_hack":          1.5,
    "j_hanging_chad":  1.0,
    "j_dusk":          1.5,
    "j_sock_and_buskin": 1.0,
    "j_selzer":       1.5,
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

# Jokers whose value derives from face cards (J/Q/K) but which are intentionally
# absent from JOKER_RANK_AFFINITY because face-card targeting is driven by the
# face_card archetype rather than hand-type affinity.
_FACE_CARD_JOKERS = frozenset({
    "j_photograph", "j_scary_face", "j_smiley",
    "j_sock_and_buskin", "j_triboulet", "j_pareidolia",
})


def _make_card(rank: str, suit: str, enhancement: str | None = None) -> Card:
    """Build a minimal synthetic Card for scoring simulation."""
    return Card(
        id=0,
        key=f"{suit}_{rank}",
        set_="DEFAULT",
        label=f"{rank} of {suit}",
        value=CardValue(rank=rank, suit=suit),
        modifier=CardModifier(enhancement=enhancement) if enhancement else CardModifier(),
    )


# ---------------------------------------------------------------------------
# Phase 9: deck-sampled Monte Carlo hand generator
# ---------------------------------------------------------------------------

_ALL_RANKS: tuple[str, ...] = ("2", "3", "4", "5", "6", "7", "8", "9", "T", "J", "Q", "K", "A")
_RANK_ORDER: dict[str, int] = {r: i for i, r in enumerate(_ALL_RANKS)}
_ALL_SUITS: tuple[str, ...] = ("H", "D", "C", "S")

# Scoring-relevant enhancements. STONE breaks hand-type constraints and is
# handled specially. WILD is suit-promiscuous. Others add chips/mult on scoring.
_SAMPLING_ENHANCEMENTS: tuple[str, ...] = (
    "BONUS", "MULT", "WILD", "GLASS", "STEEL", "GOLD", "LUCKY", "STONE",
)


def _deck_signature(dp: "DeckProfile | None") -> tuple:
    """Hashable fingerprint of a DeckProfile. Changes iff deck composition
    changes; held constant across shop ticks on the same deck state."""
    if dp is None:
        return ("vanilla",)
    return (
        dp.total_cards,
        tuple(sorted(dp.rank_counts.items())),
        tuple(sorted(dp.suit_counts.items())),
        tuple(sorted(dp.enhancement_counts.items())),
    )


def _weighted_choice(rng: random.Random, items: list, weights: list[float]):
    """Pick one item from *items* proportional to *weights*. Returns None if
    all weights are zero."""
    total = sum(weights)
    if total <= 0:
        return None
    r = rng.random() * total
    acc = 0.0
    for it, w in zip(items, weights):
        acc += w
        if r < acc:
            return it
    return items[-1]


def _sample_enhancement(dp: "DeckProfile", rng: random.Random) -> str | None:
    """Pick an enhancement for a random deck slot. None = unenhanced (the
    majority in most decks)."""
    total = dp.total_cards
    if total <= 0:
        return None
    enh_count = sum(dp.enhancement_counts.get(e, 0) for e in _SAMPLING_ENHANCEMENTS)
    if rng.random() >= enh_count / total:
        return None
    items = [e for e in _SAMPLING_ENHANCEMENTS if dp.enhancement_counts.get(e, 0) > 0]
    weights = [float(dp.enhancement_counts.get(e, 0)) for e in items]
    return _weighted_choice(rng, items, weights)


def _rank_with_min(
    dp: "DeckProfile", min_copies: int, rng: random.Random,
    *, exclude: set[str] | None = None,
) -> str | None:
    items = [r for r, c in dp.rank_counts.items()
             if c >= min_copies and (exclude is None or r not in exclude)]
    if not items:
        return None
    weights = [float(dp.rank_counts[r]) for r in items]
    return _weighted_choice(rng, items, weights)


def _stratified_ranks(
    dp: "DeckProfile", min_copies: int, n: int,
    *, exclude: set[str] | None = None,
) -> list[str]:
    """Deterministically apportion N rank slots proportionally to rank_counts.

    Eliminates the Bernoulli sampling variance on the scoring rank for
    Pair/3oK/4oK/Full House/etc. With 32 samples and an ace fraction of 8/56,
    a naive per-sample weighted draw has ~1% chance of producing zero ace
    hits — enough to flip rank-affinity valuations (e.g. Scholar, Wee).
    Stratified apportionment gives ace exactly round(32 * 8/56) = 5 slots.

    Uses the Hare / largest-remainder method: each rank gets floor(n*w/W)
    slots, leftover slots go to ranks with largest fractional parts.
    Returns [] if no rank satisfies min_copies. Output length is exactly n
    (or 0).
    """
    items = [(r, c) for r, c in dp.rank_counts.items()
             if c >= min_copies and (exclude is None or r not in exclude)]
    if not items:
        return []
    total = sum(c for _, c in items)
    if total <= 0:
        return []
    floors: list[tuple[str, int, float]] = []  # (rank, floor_slots, frac)
    allocated = 0
    for r, c in items:
        quota = n * c / total
        fl = int(quota)
        floors.append((r, fl, quota - fl))
        allocated += fl
    leftover = n - allocated
    # Distribute leftovers by largest fractional part, ties broken by rank order
    ordering = sorted(range(len(floors)), key=lambda i: (-floors[i][2], floors[i][0]))
    extra = {i: 0 for i in range(len(floors))}
    for k in range(leftover):
        extra[ordering[k % len(ordering)]] += 1
    out: list[str] = []
    for i, (r, fl, _f) in enumerate(floors):
        out.extend([r] * (fl + extra[i]))
    return out[:n]


def _suit_with_min(dp: "DeckProfile", min_copies: int, rng: random.Random) -> str | None:
    items = [s for s, c in dp.suit_counts.items() if c >= min_copies]
    if not items:
        return None
    weights = [float(dp.suit_counts[s]) for s in items]
    return _weighted_choice(rng, items, weights)


def _sample_suit(dp: "DeckProfile", rng: random.Random) -> str | None:
    items = [s for s in _ALL_SUITS if dp.suit_counts.get(s, 0) > 0]
    if not items:
        return None
    weights = [float(dp.suit_counts[s]) for s in items]
    return _weighted_choice(rng, items, weights)


def _sample_rank(dp: "DeckProfile", rng: random.Random,
                 *, exclude: set[str] | None = None) -> str | None:
    items = [r for r in _ALL_RANKS
             if dp.rank_counts.get(r, 0) > 0
             and (exclude is None or r not in exclude)]
    if not items:
        return None
    weights = [float(dp.rank_counts[r]) for r in items]
    return _weighted_choice(rng, items, weights)


def _sample_card(dp: "DeckProfile", rng: random.Random,
                 *, rank: str | None = None, suit: str | None = None,
                 exclude_ranks: set[str] | None = None) -> Card:
    r = rank if rank is not None else _sample_rank(dp, rng, exclude=exclude_ranks)
    s = suit if suit is not None else _sample_suit(dp, rng)
    enh = _sample_enhancement(dp, rng)
    return _make_card(r, s, enhancement=enh)


_STRAIGHT_WINDOWS: list[list[str]] = [
    list(_ALL_RANKS[i:i + 5]) for i in range(0, len(_ALL_RANKS) - 4)
] + [["A", "2", "3", "4", "5"]]


def _pick_straight_window(
    dp: "DeckProfile", rng: random.Random, shortcut: bool = False,
) -> list[str] | None:
    windows = list(_STRAIGHT_WINDOWS)
    if shortcut:
        # Shortcut lets the straight skip one rank. Approximate by allowing any
        # 5-rank window from a 6-rank span.
        windows += [
            [r for r in _ALL_RANKS[i:i + 6] if r not in (_ALL_RANKS[i + 2],)][:5]
            for i in range(0, len(_ALL_RANKS) - 5)
        ]
    viable = [w for w in windows if all(dp.rank_counts.get(r, 0) >= 1 for r in w)]
    if not viable:
        return None
    return rng.choice(viable)


def _sample_hand_from_deck(
    dp: "DeckProfile",
    hand_name: str,
    rng: random.Random,
    *,
    shortcut: bool = False,
    force_rank1: str | None = None,
    force_rank2: str | None = None,
) -> tuple[list[Card], list[Card]] | None:
    """Draw a (scoring_cards, played_cards) pair for *hand_name* from *dp*.

    Ranks are sampled weighted by rank_counts; suits by suit_counts;
    enhancements by enhancement_counts (each slot independent). Returns None
    when the hand type can't be formed against this deck (e.g. Flush in a
    deck with no suit >=5 copies).
    """
    if hand_name == "High Card":
        r = _sample_rank(dp, rng)
        scoring = [_sample_card(dp, rng, rank=r)]
        filler_ranks = set([r])
        filler = []
        for _ in range(4):
            fr = _sample_rank(dp, rng, exclude=filler_ranks)
            filler_ranks.add(fr)
            filler.append(_sample_card(dp, rng, rank=fr))
        return scoring, scoring + filler

    if hand_name == "Pair":
        r = force_rank1 if force_rank1 is not None else _rank_with_min(dp, 2, rng)
        if r is None:
            return None
        scoring = [_sample_card(dp, rng, rank=r) for _ in range(2)]
        filler = [_sample_card(dp, rng, exclude_ranks={r}) for _ in range(3)]
        return scoring, scoring + filler

    if hand_name == "Two Pair":
        r1 = force_rank1 if force_rank1 is not None else _rank_with_min(dp, 2, rng)
        if r1 is None:
            return None
        r2 = force_rank2 if force_rank2 is not None else _rank_with_min(dp, 2, rng, exclude={r1})
        if r2 is None or r2 == r1:
            return None
        scoring = ([_sample_card(dp, rng, rank=r1) for _ in range(2)]
                   + [_sample_card(dp, rng, rank=r2) for _ in range(2)])
        filler = [_sample_card(dp, rng, exclude_ranks={r1, r2})]
        return scoring, scoring + filler

    if hand_name == "Three of a Kind":
        r = force_rank1 if force_rank1 is not None else _rank_with_min(dp, 3, rng)
        if r is None:
            return None
        scoring = [_sample_card(dp, rng, rank=r) for _ in range(3)]
        filler = [_sample_card(dp, rng, exclude_ranks={r}) for _ in range(2)]
        return scoring, scoring + filler

    if hand_name == "Straight":
        window = _pick_straight_window(dp, rng, shortcut=shortcut)
        if window is None:
            return None
        scoring = [_sample_card(dp, rng, rank=r) for r in window]
        return scoring, list(scoring)

    if hand_name == "Flush":
        s = _suit_with_min(dp, 5, rng)
        if s is None:
            return None
        # 5 distinct ranks preferred; relax if deck can't supply.
        chosen_ranks: list[str] = []
        tried: set[str] = set()
        for _ in range(5):
            r = _sample_rank(dp, rng, exclude=set(chosen_ranks) | tried)
            if r:
                chosen_ranks.append(r)
                tried.add(r)
        while len(chosen_ranks) < 5:
            chosen_ranks.append(_sample_rank(dp, rng))
        scoring = [_sample_card(dp, rng, rank=r, suit=s) for r in chosen_ranks]
        return scoring, list(scoring)

    if hand_name == "Full House":
        r3 = force_rank1 if force_rank1 is not None else _rank_with_min(dp, 3, rng)
        if r3 is None:
            return None
        r2 = force_rank2 if force_rank2 is not None else _rank_with_min(dp, 2, rng, exclude={r3})
        if r2 is None or r2 == r3:
            return None
        scoring = ([_sample_card(dp, rng, rank=r3) for _ in range(3)]
                   + [_sample_card(dp, rng, rank=r2) for _ in range(2)])
        return scoring, list(scoring)

    if hand_name == "Four of a Kind":
        r = force_rank1 if force_rank1 is not None else _rank_with_min(dp, 4, rng)
        if r is None:
            return None
        scoring = [_sample_card(dp, rng, rank=r) for _ in range(4)]
        filler = [_sample_card(dp, rng, exclude_ranks={r})]
        return scoring, scoring + filler

    if hand_name == "Straight Flush":
        s = _suit_with_min(dp, 5, rng)
        if s is None:
            return None
        window = _pick_straight_window(dp, rng, shortcut=shortcut)
        if window is None:
            return None
        scoring = [_sample_card(dp, rng, rank=r, suit=s) for r in window]
        return scoring, list(scoring)

    if hand_name == "Flush Five":
        r = force_rank1 if force_rank1 is not None else _rank_with_min(dp, 5, rng)
        s = _suit_with_min(dp, 5, rng)
        if r is None or s is None:
            return None
        scoring = [_sample_card(dp, rng, rank=r, suit=s) for _ in range(5)]
        return scoring, list(scoring)

    if hand_name == "Five of a Kind":
        r = force_rank1 if force_rank1 is not None else _rank_with_min(dp, 5, rng)
        if r is None:
            return None
        scoring = [_sample_card(dp, rng, rank=r) for _ in range(5)]
        return scoring, list(scoring)

    if hand_name == "Flush House":
        r3 = force_rank1 if force_rank1 is not None else _rank_with_min(dp, 3, rng)
        if r3 is None:
            return None
        r2 = force_rank2 if force_rank2 is not None else _rank_with_min(dp, 2, rng, exclude={r3})
        if r2 is None or r2 == r3:
            return None
        s = _suit_with_min(dp, 5, rng)
        if s is None:
            return None
        scoring = ([_sample_card(dp, rng, rank=r3, suit=s) for _ in range(3)]
                   + [_sample_card(dp, rng, rank=r2, suit=s) for _ in range(2)])
        return scoring, list(scoring)

    # Unknown hand type — fall back to High Card.
    return _sample_hand_from_deck(dp, "High Card", rng, shortcut=shortcut)


def _sample_held_from_deck(
    dp: "DeckProfile", rng: random.Random, n: int = 3,
) -> list[Card]:
    """Draw held-phase cards independently from the deck. Baron/SttM/RF/Mime
    fire iff their relevant rank shows up — same as gameplay."""
    return [_sample_card(dp, rng) for _ in range(n)]


# Module-level sample cache. Key = (deck_sig, hand_type); value = list of
# (scoring, played) tuples for sample indices 0..N-1. Invalidates automatically
# when the deck mutates (new deck_sig). Bounded to keep value_map.py's many
# synthetic decks from blowing memory.
_HAND_SAMPLE_CACHE: dict[tuple, list[tuple[list[Card], list[Card]]]] = {}
_HELD_SAMPLE_CACHE: dict[tuple, list[list[Card]]] = {}
_SAMPLE_CACHE_MAX = 2048


def _trim_cache(cache: dict) -> None:
    if len(cache) > _SAMPLE_CACHE_MAX:
        drop = len(cache) - _SAMPLE_CACHE_MAX // 2
        for k in list(cache.keys())[:drop]:
            cache.pop(k, None)


_VANILLA_DECK_FOR_SIM: "DeckProfile | None" = None


def _get_vanilla_deck() -> "DeckProfile":
    """Lazy-built 52-card standard deck profile, used when no deck_profile is
    supplied (tests, bare-roster valuations)."""
    global _VANILLA_DECK_FOR_SIM
    if _VANILLA_DECK_FOR_SIM is None:
        from balatro_bot.domain.models.deck_profile import DeckProfile
        _VANILLA_DECK_FOR_SIM = DeckProfile(
            total_cards=52,
            rank_counts={r: 4 for r in _ALL_RANKS},
            suit_counts={s: 13 for s in _ALL_SUITS},
            enhancement_counts={},
            enhanced_card_count=0,
        )
    return _VANILLA_DECK_FOR_SIM


def _get_hand_samples(
    dp: "DeckProfile | None", hand_name: str, n: int,
) -> list[tuple[list[Card], list[Card]]]:
    """Cached list of up to *n* sampled hands for (deck_sig, hand_name).

    Same deck + same hand_type yields the same samples across all candidates
    and all ticks — this is what makes CRN work and keeps shop decisions
    stable as the evaluator re-runs within a single shop visit.
    """
    deck = dp if dp is not None else _get_vanilla_deck()
    sig = _deck_signature(deck)
    key = (sig, hand_name)
    cached = _HAND_SAMPLE_CACHE.get(key)
    if cached is not None and len(cached) >= n:
        return cached[:n]
    # Stratify the scoring rank for n-of-a-kind hands to eliminate Bernoulli
    # variance on which rank forms the pair/trips/quads. Without this, 32
    # samples against an 8/56 ace fraction has a ~1% chance of producing zero
    # ace-pairs — enough to flip rank-affinity valuations.
    forced_ranks1: list[str | None] = [None] * n
    forced_ranks2: list[str | None] = [None] * n
    if hand_name in ("Pair", "Two Pair"):
        strat = _stratified_ranks(deck, 2, n)
        for i, r in enumerate(strat):
            forced_ranks1[i] = r
    elif hand_name in ("Three of a Kind", "Full House", "Flush House"):
        strat = _stratified_ranks(deck, 3, n)
        for i, r in enumerate(strat):
            forced_ranks1[i] = r
    elif hand_name == "Four of a Kind":
        strat = _stratified_ranks(deck, 4, n)
        for i, r in enumerate(strat):
            forced_ranks1[i] = r
    elif hand_name in ("Five of a Kind", "Flush Five"):
        strat = _stratified_ranks(deck, 5, n)
        for i, r in enumerate(strat):
            forced_ranks1[i] = r

    # For two-rank hands, stratify the secondary rank conditioned on the primary.
    if hand_name in ("Two Pair", "Full House", "Flush House"):
        # Group sample indices by primary rank, then stratify secondary
        # (min=2, excluding the primary) within each group.
        groups: dict[str, list[int]] = {}
        for i, r in enumerate(forced_ranks1):
            if r is None:
                continue
            groups.setdefault(r, []).append(i)
        for r1, idxs in groups.items():
            strat2 = _stratified_ranks(deck, 2, len(idxs), exclude={r1})
            for j, i in enumerate(idxs):
                if j < len(strat2):
                    forced_ranks2[i] = strat2[j]

    # Build up to n samples from scratch (re-seed each for determinism).
    samples: list[tuple[list[Card], list[Card]]] = []
    for i in range(n):
        seed = zlib.crc32(f"{sig}|{hand_name}|{i}".encode())
        sampled = _sample_hand_from_deck(
            deck, hand_name, random.Random(seed),
            force_rank1=forced_ranks1[i], force_rank2=forced_ranks2[i],
        )
        if sampled is None:
            continue
        samples.append(sampled)
    _HAND_SAMPLE_CACHE[key] = samples
    _trim_cache(_HAND_SAMPLE_CACHE)
    return samples


def _get_held_samples(
    dp: "DeckProfile | None", n: int, held_count: int = 3,
) -> list[list[Card]]:
    deck = dp if dp is not None else _get_vanilla_deck()
    sig = _deck_signature(deck)
    key = (sig, held_count)
    cached = _HELD_SAMPLE_CACHE.get(key)
    if cached is not None and len(cached) >= n:
        return cached[:n]
    samples: list[list[Card]] = []
    for i in range(n):
        seed = zlib.crc32(f"{sig}|_held|{i}".encode())
        samples.append(_sample_held_from_deck(deck, random.Random(seed), n=held_count))
    _HELD_SAMPLE_CACHE[key] = samples
    _trim_cache(_HELD_SAMPLE_CACHE)
    return samples


def clear_sample_cache() -> None:
    """Test helper / explicit invalidation when deck has changed in-place."""
    _HAND_SAMPLE_CACHE.clear()
    _HELD_SAMPLE_CACHE.clear()


def _idol_context_rank(ctx: SimContext) -> str:
    """Approximate rank for Ancient/Idol procs: deck-dominant rank biased by
    strategy preference. In-game these are round-variable; the bot plays to
    match, so the sim pins to the most plausible target."""
    if ctx.strategy and ctx.strategy.preferred_ranks:
        return ctx.strategy.preferred_ranks[0][0]
    if ctx.deck_profile is not None and ctx.deck_profile.rank_counts:
        return max(ctx.deck_profile.rank_counts.items(), key=lambda kv: kv[1])[0]
    return "A"


def _idol_context_suit(ctx: SimContext) -> str:
    if ctx.strategy and ctx.strategy.preferred_suits:
        return ctx.strategy.preferred_suits[0][0]
    if ctx.deck_profile is not None and ctx.deck_profile.suit_counts:
        return max(ctx.deck_profile.suit_counts.items(), key=lambda kv: kv[1])[0]
    return "H"


# ---------------------------------------------------------------------------
# Legacy synthetic-hand builder (fallback when _MC_SAMPLING_ENABLED=0)
# ---------------------------------------------------------------------------

def _preferred_suit(ctx: SimContext) -> str:
    candidate_key = ctx.candidate_key
    strategy = ctx.strategy
    if candidate_key:
        cand_suit = JOKER_SUIT_AFFINITY.get(candidate_key)
        if cand_suit and cand_suit[1] > 0:
            return cand_suit[0]
        if candidate_key in ("j_ancient", "j_idol"):
            # Round-variable — assume Hearts (matches default, common scoring).
            return "H"
    if strategy and strategy.preferred_suits:
        return strategy.preferred_suits[0][0]
    return _DEFAULT_SUIT


def _preferred_rank(ctx: SimContext) -> str:
    candidate_key = ctx.candidate_key
    strategy = ctx.strategy
    # Candidate's own rank affinity wins — evaluating Scholar should produce aces
    # even against a roster whose preferred_ranks point elsewhere.
    if candidate_key:
        cand_ranks = JOKER_RANK_AFFINITY.get(candidate_key)
        if cand_ranks and cand_ranks[1] > 0:
            return cand_ranks[0][0]
        if candidate_key in _FACE_CARD_JOKERS:
            return "K"
    if strategy and strategy.has_archetype("face_card"):
        return "K"
    if strategy and strategy.preferred_ranks:
        return strategy.preferred_ranks[0][0]
    return _TYPICAL_RANK


def _alt_suit(primary: str) -> str:
    """Return a suit different from primary."""
    return "D" if primary != "D" else "C"


# Enhancement awareness — which jokers want which enhancement on scoring cards
# so their per-card triggers fire inside the sim. Steel is excluded (Steel on a
# scoring card is a no-op; Steel Joker's xmult is projected separately).
_ENH_RELEVANCE: dict[str, str] = {
    "j_lucky_cat": "LUCKY",
    "j_glass":     "GLASS",
}

# Enhancements that contribute chips/mult/xmult when on a scoring card. Ordered
# by "intrinsic value" so Steel doesn't crowd out Lucky/Glass/Bonus/Mult when
# deck densities compete for slots.
_SCORING_ENH_PRIORITY: tuple[str, ...] = (
    "GLASS", "LUCKY", "MULT", "BONUS", "STONE", "GOLD",
)


def _plan_enhancements(ctx: "SimContext", n_scoring: int) -> list[str | None]:
    """Assign enhancements to scoring-card slots via analytic deck-density EV.

    Slot i gets at most one enhancement; relevant enhancements (those any
    owned/candidate joker cares about) are guaranteed one slot when present in
    deck, even if density × n_scoring rounds to 0. Otherwise slots are filled
    proportionally to deck density.
    """
    plan: list[str | None] = [None] * n_scoring
    if n_scoring == 0 or not ctx.enhancement_density:
        return plan

    relevant = ctx.owned_keys | {ctx.candidate_key}
    forced: list[str] = [
        enh for jk, enh in _ENH_RELEVANCE.items()
        if jk in relevant and ctx.enhancement_density.get(enh, 0.0) > 0.0
    ]

    used = 0

    # Step 1: guarantee relevant enhancements get a slot so the joker's per-card
    # effect fires in the sim even at low density (e.g. 4 Lucky in 52 = 0.077
    # rounds to 0 slots for a Pair but still gives Lucky Cat something to chew).
    for enh in forced:
        if used >= n_scoring:
            break
        plan[used] = enh
        used += 1

    # Step 2: fill remaining slots proportional to deck density across the
    # priority list. Skip Steel (no scoring effect) and anything already forced.
    for enh in _SCORING_ENH_PRIORITY:
        if enh in forced:
            continue
        density = ctx.enhancement_density.get(enh, 0.0)
        if density <= 0:
            continue
        slots = round(n_scoring * density)
        for _ in range(slots):
            if used >= n_scoring:
                break
            plan[used] = enh
            used += 1
        if used >= n_scoring:
            break

    return plan


# Rank-per-card jokers whose effect fires once per scored card of a target
# rank. Value is proportional to how often those ranks appear in the deck.
_RANK_PER_CARD_JOKERS: dict[str, tuple[str, ...]] = {
    "j_wee":            ("2",),
    "j_scholar":        ("A",),
    "j_fibonacci":      ("A", "2", "3", "5", "8"),
    "j_even_steven":    ("2", "4", "6", "8", "T"),
    "j_odd_todd":       ("A", "3", "5", "7", "9"),
    "j_triboulet":      ("K", "Q"),
    "j_walkie_talkie":  ("T", "4"),
    "j_hack":           ("2", "3", "4", "5"),  # retrigger
}


def _rank_density_factor(ctx: "SimContext", key: str) -> float:
    """Scale a rank-per-card joker's sim delta by how dense its target ranks
    are in the real deck. Baseline = 4 copies per rank (vanilla deck)."""
    ranks = _RANK_PER_CARD_JOKERS.get(key)
    if not ranks or ctx.deck_profile is None:
        return 1.0
    baseline = len(ranks) * 4
    if baseline == 0:
        return 1.0
    count = sum(ctx.deck_profile.rank_counts.get(r, 0) for r in ranks)
    return min(count / baseline, 2.0)


def _apply_enh_plan(cards: list, plan: list[str | None]) -> list:
    """Return a copy of *cards* with enhancements from *plan* applied slot-wise."""
    out = []
    for i, c in enumerate(cards):
        enh = plan[i] if i < len(plan) else None
        if enh:
            out.append(_make_card(c.value.rank, c.value.suit, enhancement=enh))
        else:
            out.append(c)
    return out


def _synthetic_hand_base(
    ctx: SimContext,
    hand_name: str,
) -> tuple[list[dict], list[dict]]:
    """Unenhanced scoring + played cards for the given hand type.

    Kept separate from _synthetic_hand so the enhancement planner can reshape
    the scoring subset without duplicating hand-type branching.
    """
    suit = _preferred_suit(ctx)
    rank = _preferred_rank(ctx)
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


def _synthetic_hand(
    ctx: SimContext,
    hand_name: str,
) -> tuple[list[dict], list[dict]]:
    """Build (scoring_cards, played_cards) and decorate scoring with
    density-planned enhancements so per-card enhancement jokers (Lucky Cat,
    Glass, Vampire, …) fire inside the sim instead of via post-hoc adjustment.
    """
    scoring, played = _synthetic_hand_base(ctx, hand_name)
    plan = _plan_enhancements(ctx, len(scoring))
    if not any(plan):
        return scoring, played

    original_ids = {id(c) for c in scoring}
    scoring_new = _apply_enh_plan(scoring, plan)
    old_to_new = {id(old): new for old, new in zip(scoring, scoring_new)}
    played_new = [old_to_new[id(c)] if id(c) in original_ids else c for c in played]
    return scoring_new, played_new


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


def _sim_coefficient(key: str) -> float:
    """Pick the per-category sim coefficient for a joker key."""
    cat = _KEY_TO_CATEGORY.get(key)
    if cat == "xmult":
        return SIM_COEFF_XMULT
    if cat == "mult":
        return SIM_COEFF_MULT
    if cat == "chips":
        return SIM_COEFF_CHIPS
    return SIM_COEFF_DEFAULT


_PER_CARD_SCORING_JOKERS = frozenset({
    "j_greedy_joker", "j_lusty_joker", "j_wrathful_joker", "j_gluttenous_joker",
    "j_fibonacci", "j_even_steven", "j_odd_todd", "j_scholar",
    "j_smiley", "j_scary_face", "j_walkie_talkie",
    "j_photograph", "j_triboulet", "j_ancient",
    "j_arrowhead", "j_onyx_agate", "j_bloodstone", "j_idol", "j_hiker",
    # Retrigger jokers — fire via retrigger_count() during per-card scoring,
    # not via JOKER_EFFECTS dispatch, but they DO produce real sim deltas.
    "j_hanging_chad", "j_hack", "j_sock_and_buskin",
    "j_dusk", "j_selzer",
})

# Held-in-hand phase jokers. These are _noop in JOKER_EFFECTS but fire via
# the held-card loop in estimate._apply_card_scoring. Sim sees them only when
# ctx.held_cards is non-empty — see _synthetic_held_cards().
_HELD_SCORING_JOKERS = frozenset({
    "j_baron", "j_shoot_the_moon", "j_raised_fist", "j_mime",
    # Blackboard already routes (has a complex effect), listed for clarity.
    "j_blackboard",
})


def _has_scoring_effect(key: str) -> bool:
    if key in _PER_CARD_SCORING_JOKERS:
        return True
    if key in _HELD_SCORING_JOKERS:
        return True
    effect = JOKER_EFFECTS.get(key)
    return effect is not None and effect is not _noop


def _synthetic_held_cards(ctx: SimContext) -> list[Card]:
    """Build synthetic held cards for held-phase joker sim.

    Returns 0-3 cards based on which held-phase jokers are in owned+candidate.
    Drives Baron (Kings), Shoot the Moon (Queens), Raised Fist (low card),
    Mime (retrigger), Blackboard (all S/C).
    """
    relevant = ctx.owned_keys | {ctx.candidate_key}
    has_baron = "j_baron" in relevant
    has_sttm = "j_shoot_the_moon" in relevant
    has_rf = "j_raised_fist" in relevant
    has_mime = "j_mime" in relevant
    has_bb = "j_blackboard" in relevant

    if not (has_baron or has_sttm or has_rf or has_mime or has_bb):
        return []

    suit = "S" if has_bb else _preferred_suit(ctx)
    alt = "C" if has_bb else _alt_suit(suit)

    # Baron + SttM together — mix Kings and Queens.
    if has_baron and has_sttm:
        return [_make_card("K", suit), _make_card("K", alt), _make_card("Q", suit)]
    if has_baron:
        return [_make_card("K", suit), _make_card("K", alt), _make_card("K", suit)]
    if has_sttm:
        return [_make_card("Q", suit), _make_card("Q", alt), _make_card("Q", suit)]
    if has_rf:
        # Lowest-held drives RF value — pick "2" plus two filler.
        return [_make_card("2", suit), _make_card("5", alt), _make_card("7", suit)]
    if has_mime:
        # Mime alone retriggers whatever's held — give it face cards to chew.
        return [_make_card("K", suit), _make_card("Q", alt), _make_card("J", suit)]
    if has_bb:
        # Blackboard with no other held-phase signal — all held S/C.
        return [_make_card("T", suit), _make_card("9", alt), _make_card("8", suit)]
    return []


# Deck-density xmult scalers — their parsed effect text is X1.0 at buy-time so
# the raw sim sees zero delta. Project a forward xmult from deck composition
# and feed it to the sim via a rewritten candidate copy. Previously handled by
# the flat ``_deck_composition_adjustment`` post-hoc boost.
_DECK_DENSITY_XMULT_EXTRA: dict[str, tuple[str, float]] = {
    # joker_key -> (enhancement_counted, xmult_per_card)
    "j_steel_joker": ("STEEL", 0.2),   # +X0.2 per Steel in deck (game formula)
    "j_glass":       ("GLASS", 0.75),  # +X0.75 per Glass destroyed (EV via density below)
}

# Fraction of matching enhanced cards expected to realize their trigger over
# the remaining runway. Steel is static (every scoring is multiplied) so it's
# 1.0. Glass needs cards to break — conservative 0.3 of deck count as an EV.
_DECK_DENSITY_REALIZATION: dict[str, float] = {
    "j_steel_joker": 1.0,
    "j_glass": 0.3,
}


def _project_deck_density_candidate(ctx: SimContext) -> dict:
    """If the candidate is a deck-density xmult scaler, return a dict-copy with
    its effect text rewritten to project its xmult from the current deck's
    enhancement count. Otherwise return the candidate unchanged.
    """
    key = ctx.candidate_key
    spec = _DECK_DENSITY_XMULT_EXTRA.get(key)
    if spec is None or ctx.deck_profile is None:
        return ctx.candidate
    enh, per = spec
    count = ctx.deck_profile.enhancement_counts.get(enh, 0)
    if count <= 0:
        return ctx.candidate
    realization = _DECK_DENSITY_REALIZATION.get(key, 1.0)
    projected = 1.0 + per * count * realization
    if projected <= 1.0:
        return ctx.candidate
    new = dict(ctx.candidate)
    val = dict(ctx.candidate.get("value", {}))
    val["effect"] = f"X{projected:.2f} Mult"
    new["value"] = val
    return new


def _scoring_delta(
    ctx: SimContext,
    hand_types: list[tuple[str, float]],
) -> float:
    """Score with/without candidate across weighted hand types.

    Returns a weighted fractional improvement (0.5 = 50% average improvement).
    Filters the candidate out of owned_jokers so sell evaluations measure the
    true marginal value (not the value of a duplicate).
    """
    if _MC_SAMPLING_ENABLED:
        return _scoring_delta_sampled(ctx, hand_types)
    return _scoring_delta_legacy(ctx, hand_types)


def _scoring_delta_sampled(
    ctx: SimContext,
    hand_types: list[tuple[str, float]],
) -> float:
    """Deck-sampled Monte Carlo path. Each hand type draws N hands from the
    current DeckProfile. Hands the deck can't form (e.g. Flush in a stripped
    deck) are dropped and the remaining weight is re-normalized — a joker
    whose sim relies on that hand type correctly reads ~0 contribution."""
    total_weight = sum(w for _, w in hand_types)
    if total_weight <= 0:
        return 0.0

    candidate = _project_deck_density_candidate(ctx)
    owned_jokers = ctx.owned_jokers
    hand_levels = ctx.hand_levels
    joker_limit = ctx.joker_limit
    candidate_key = ctx.candidate_key
    owned_keys = set(ctx.owned_keys)
    candidate_keys = owned_keys | {candidate_key}

    if "j_card_sharp" in candidate_keys:
        hand_levels = {
            h: {**v, "played_this_round": max(1, v.get("played_this_round", 0))}
            for h, v in hand_levels.items()
        }

    sim_discards_left = ctx.discards_left
    if "j_mystic_summit" in candidate_keys:
        sim_discards_left = 0

    baseline_jokers = [j for j in owned_jokers if j is not ctx.candidate]

    def _place_candidate(base: list[dict]) -> list[dict]:
        if not base:
            return [candidate]
        copyable = [
            (i, j) for i, j in enumerate(base)
            if joker_key(j) not in BLUEPRINT_INCOMPATIBLE
            and joker_key(j) not in ("j_blueprint", "j_brainstorm")
        ]
        if not copyable:
            return base + [candidate]
        if candidate_key == "j_blueprint":
            target_i, _ = copyable[-1]
            return base[:target_i] + [candidate] + base[target_i:]
        if candidate_key == "j_brainstorm":
            target_i, target_j = copyable[0]
            rest = [j for k, j in enumerate(base) if k != target_i]
            return [target_j, candidate] + rest
        return base + [candidate]

    # Ancient/Idol need a rank+suit pinned for their effect to fire at all —
    # model the bot playing to match the round's target.
    ancient_suit = _idol_context_suit(ctx) if "j_ancient" in candidate_keys else None
    idol_rank = _idol_context_rank(ctx) if "j_idol" in candidate_keys else None
    idol_suit = _idol_context_suit(ctx) if "j_idol" in candidate_keys else None

    baseline_jokers = reorder_for_scoring(baseline_jokers)
    with_jokers = reorder_for_scoring(_place_candidate(list(baseline_jokers)))

    # N=1 for candidates whose contribution doesn't vary with card composition
    # (pure runway projections or global-state multipliers).
    n_samples = _MC_AGNOSTIC_SAMPLES if candidate_key in _CARD_AGNOSTIC else _MC_FULL_SAMPLES

    weighted_delta = 0.0
    total_used_weight = 0.0

    for hand_name, weight in hand_types:
        samples = _get_hand_samples(ctx.deck_profile, hand_name, n_samples)
        if not samples:
            # Deck can't form this hand — skip; other hand types carry the
            # weight. Correctly zeros Flush value in stripped decks, etc.
            continue
        held_samples = _get_held_samples(
            ctx.deck_profile, len(samples), held_count=3,
        )

        base_sum = 0.0
        cand_sum = 0.0
        for s_idx, (scoring_cards, played_cards) in enumerate(samples):
            held = held_samples[s_idx] if s_idx < len(held_samples) else []

            # Half Joker caps played at the scoring subset.
            local_played = played_cards
            if "j_half" in candidate_keys and len(scoring_cards) <= 3:
                local_played = list(scoring_cards)

            seed = zlib.crc32(f"{candidate_key}|{hand_name}|{s_idx}|mc".encode())
            base_sum += score_hand(
                hand_name, scoring_cards, hand_levels,
                jokers=baseline_jokers, played_cards=local_played,
                held_cards=held, joker_limit=joker_limit,
                ancient_suit=ancient_suit, idol_rank=idol_rank, idol_suit=idol_suit,
                money=ctx.money, discards_left=sim_discards_left,
                rng=random.Random(seed),
            )[2]
            cand_sum += score_hand(
                hand_name, scoring_cards, hand_levels,
                jokers=with_jokers, played_cards=local_played,
                held_cards=held, joker_limit=joker_limit,
                ancient_suit=ancient_suit, idol_rank=idol_rank, idol_suit=idol_suit,
                money=ctx.money, discards_left=sim_discards_left,
                rng=random.Random(seed),
            )[2]

        baseline_total = base_sum / len(samples)
        candidate_total = cand_sum / len(samples)
        base_total = max(baseline_total, 1)
        delta = (candidate_total - baseline_total) / base_total
        weighted_delta += delta * weight
        total_used_weight += weight

    if total_used_weight <= 0:
        return 0.0
    return weighted_delta / total_used_weight


def _scoring_delta_legacy(
    ctx: SimContext,
    hand_types: list[tuple[str, float]],
) -> float:
    total_weight = sum(w for _, w in hand_types)
    if total_weight <= 0:
        return 0.0

    candidate = _project_deck_density_candidate(ctx)
    owned_jokers = ctx.owned_jokers
    hand_levels = ctx.hand_levels
    joker_limit = ctx.joker_limit
    strategy = ctx.strategy

    # Card Sharp only fires when the played hand type already has
    # played_this_round > 0. Fresh hand_levels dicts always have 0, so the
    # sim would zero Card Sharp out. Synthesize a repeat-play state for this
    # evaluation — the proc-rate correction in evaluate_joker_value then
    # discounts for the fact that not every hand is a repeat.
    candidate_key_early = ctx.candidate_key
    if "j_card_sharp" in (set(ctx.owned_keys) | {candidate_key_early}):
        hand_levels = {
            h: {**v, "played_this_round": max(1, v.get("played_this_round", 0))}
            for h, v in hand_levels.items()
        }

    # Mystic Summit fires at discards_left <= 0. With Mystic in the roster
    # the bot actively burns discards to 0 to guarantee the proc on the
    # finisher. Force discards_left=0 for this eval; proc-rate correction
    # in evaluate_joker_value then discounts for the fact that not every
    # hand of a round is at zero discards.
    sim_discards_left = ctx.discards_left
    if "j_mystic_summit" in (set(ctx.owned_keys) | {candidate_key_early}):
        sim_discards_left = 0

    # Filter candidate from owned to handle sell evaluations correctly
    candidate_key = ctx.candidate_key
    baseline_jokers = [j for j in owned_jokers if j is not ctx.candidate]

    # Blueprint/Brainstorm only produce value when positioned to copy a
    # compatible joker. Appending to the end (Blueprint) or leaving leftmost
    # unchanged (Brainstorm) can yield a sim value of zero even when the
    # candidate would be strong in practice. Place the copy joker adjacent to
    # the best compatible target.
    def _place_candidate(base: list[dict]) -> list[dict]:
        if not base:
            return [candidate]
        copyable = [
            (i, j) for i, j in enumerate(base)
            if joker_key(j) not in BLUEPRINT_INCOMPATIBLE
            and joker_key(j) not in ("j_blueprint", "j_brainstorm")
        ]
        if not copyable:
            return base + [candidate]
        if candidate_key == "j_blueprint":
            target_i, _ = copyable[-1]  # rightmost compatible
            return base[:target_i] + [candidate] + base[target_i:]
        if candidate_key == "j_brainstorm":
            target_i, target_j = copyable[0]  # leftmost compatible
            # Ensure the target sits at index 0, Brainstorm right after.
            rest = [j for k, j in enumerate(base) if k != target_i]
            return [target_j, candidate] + rest
        return base + [candidate]

    weighted_delta = 0.0

    # Contextual assumptions for round-variable per-card jokers during valuation:
    # pick the synthetic hand's preferred rank/suit so the effect actually fires.
    sim_suit = _preferred_suit(ctx)
    sim_rank = _preferred_rank(ctx)
    owned_keys = set(ctx.owned_keys)
    candidate_keys = owned_keys | {candidate_key}
    ancient_suit = sim_suit if "j_ancient" in candidate_keys else None
    idol_rank = sim_rank if "j_idol" in candidate_keys else None
    idol_suit = sim_suit if "j_idol" in candidate_keys else None

    want_flower_pot = "j_flower_pot" in candidate_keys
    want_seeing_double = "j_seeing_double" in candidate_keys

    held_cards = _synthetic_held_cards(ctx)

    def _rewrite_suits(cards: list, pattern: list[str]) -> list:
        out = []
        for i, c in enumerate(cards):
            new_suit = pattern[i] if i < len(pattern) else pattern[-1]
            enh = c.modifier.enhancement if c.modifier.enhancement else None
            out.append(_make_card(c.value.rank, new_suit, enhancement=enh))
        return out

    # Phase 8: normalize joker order to match the live bot's
    # ReorderJokersForScoring (chips→mult→xmult). Without this the sim scores
    # in owned order and understates jokers that benefit from being placed
    # rightmost (Hologram after chips/mult) or left of xmult (Blueprint).
    # Joker order doesn't depend on hand type, so reorder once outside the loop.
    baseline_jokers = reorder_for_scoring(baseline_jokers)
    with_jokers = reorder_for_scoring(_place_candidate(list(baseline_jokers)))

    samples = max(0, ctx.monte_carlo_samples)

    for hand_name, weight in hand_types:
        scoring_cards, played_cards = _synthetic_hand(ctx, hand_name)

        # Flower Pot needs 4+ scoring cards spanning all 4 suits. Seeing Double
        # needs Club + non-Club. Reshape suits so the condition can fire in
        # whichever hand types satisfy the card-count requirement.
        if want_flower_pot and len(scoring_cards) >= 4:
            scoring_cards = _rewrite_suits(scoring_cards, ["H", "D", "C", "S", "H"])
            played_cards = scoring_cards + played_cards[len(scoring_cards):]
        elif want_seeing_double and len(scoring_cards) >= 2:
            scoring_cards = _rewrite_suits(scoring_cards, ["C"] + ["H"] * (len(scoring_cards) - 1))
            played_cards = scoring_cards + played_cards[len(scoring_cards):]

        # Half Joker fires when played_cards <= 3. Live bot doesn't pad past
        # the scoring subset when Half is in the roster (FEWER_CARDS_JOKERS
        # in rules/_helpers.py). Mirror that here so Half actually fires in
        # the sim on hands where scoring ≤ 3 (High Card, Pair, 3-of-a-Kind).
        if "j_half" in candidate_keys and len(scoring_cards) <= 3:
            played_cards = list(scoring_cards)

        def _score(jokers: list, rng=None) -> tuple[int, int, int]:
            return score_hand(
                hand_name, scoring_cards, hand_levels,
                jokers=jokers, played_cards=played_cards,
                held_cards=held_cards,
                joker_limit=joker_limit,
                ancient_suit=ancient_suit, idol_rank=idol_rank, idol_suit=idol_suit,
                money=ctx.money, discards_left=sim_discards_left,
                rng=rng,
            )

        if samples > 0:
            # Common random numbers: same seed for baseline and candidate so
            # paired-difference variance shrinks (shared noise on Misprint /
            # Lucky Cat / Bloodstone rolls cancels between the two sims).
            base_sum = 0.0
            cand_sum = 0.0
            for s in range(samples):
                seed = zlib.crc32(f"{candidate_key}|{hand_name}|{s}".encode())
                base_sum += _score(baseline_jokers, random.Random(seed))[2]
                cand_sum += _score(with_jokers, random.Random(seed))[2]
            baseline_total = base_sum / samples
            candidate_total = cand_sum / samples
        else:
            baseline_total = _score(baseline_jokers)[2]
            candidate_total = _score(with_jokers)[2]

        base_total = max(baseline_total, 1)
        delta = (candidate_total - baseline_total) / base_total
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
_PROBABILITY_JOKERS = frozenset({
    "j_oops", "j_lucky_cat", "j_bloodstone", "j_8_ball",
    "j_space", "j_sixth_sense",
})
_HAND_CONDITIONAL_XMULT = frozenset({
    "j_duo", "j_trio", "j_tribe", "j_family", "j_order",
})

# Discount for xmult jokers whose trigger is conditional on hand type —
# copying Tribe at X2 is worth ~half the value of copying Cavendish at X2,
# since the copier only fires when the trigger hand is played.
_CONDITIONAL_XMULT_DISCOUNT = 0.5


def _copy_target_score(j: dict) -> float:
    """Score a joker as a Blueprint/Brainstorm copy target.

    Higher = better target. PHASE_XMULT beats PHASE_MULT beats everything else.
    Within PHASE_XMULT we rank by live parsed xmult (captures scaling like
    Constellation X5.5 beating Cavendish X3.0), discounted for conditional
    triggers that may not fire at scoring time.

    Reads ``self.ability`` via the effect-text parser, so scaling jokers
    contribute their *live* accumulated value, not a base estimate.
    """
    key = joker_key(j)
    if key in BLUEPRINT_INCOMPATIBLE:
        return -1.0
    phase = get_joker_phase(key)
    if phase == PHASE_XMULT:
        xm = _get_parsed_value(j, "xmult", 1.5)
        if key in CONDITIONAL_XMULT:
            xm *= _CONDITIONAL_XMULT_DISCOUNT
        # Scale into a tier above PHASE_MULT scoring; +100 keeps any xmult
        # target ranked above even huge flat-mult jokers.
        return 100.0 + xm
    if phase == PHASE_MULT:
        return _get_parsed_value(j, "mult", 0.0)
    return 0.0


def _best_copy_target_value(owned_jokers: list[dict]) -> float:
    """Return the raw xmult-equivalent of the current best copy target.

    Strips the +100 tier offset so callers can map directly to a multiplier.
    Returns 0.0 if the roster has nothing copyable (empty, all utility, or
    all copiers).
    """
    best = 0.0
    for j in owned_jokers:
        k = joker_key(j)
        if k in _COPY_JOKERS:
            continue
        s = _copy_target_score(j)
        if s >= 100.0:
            # PHASE_XMULT tier: strip the +100 tier offset
            xm = s - 100.0
            if xm > best:
                best = xm
        elif s > 0:
            # PHASE_MULT: convert flat mult to xmult-equivalent (+30 mult ~ X3
            # as a copy target, so divide by 10 for parity with xmult values).
            xm_eq = s / 10.0
            if xm_eq > best:
                best = xm_eq
    return best


def _copier_base_value(ante: int) -> float:
    """Prospective multiplier for a copier with no current target.

    Past ante 3 you're not going to win without pairing a copier with a
    scorer — buy Blueprint and assume something decent will show up. Early,
    an untargeted copier is a slot waste.
    """
    if ante <= 2:
        return 0.9
    if ante == 3:
        return 1.0
    if ante <= 5:
        return 1.15
    return 1.25


def _copier_amplification(copy_value: float) -> float:
    """Map a live copy-target value (xmult-equivalent) to a multiplier bonus.

    Cavendish X3 live → +0.30 (total ~1.30×, matches old flat anchor)
    Constellation live X5+ → +0.50 (capped)
    Duo post-discount X1 → +0.10 (conditional, smaller bonus — correct)
    Empty roster → +0.0
    """
    return min(0.5, copy_value / 10.0)


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
    elif key in _PROBABILITY_JOKERS:
        partners = (owned_keys & _PROBABILITY_JOKERS) - {key}
        bonus += len(partners) * 1.5
        if key != "j_oops" and "j_oops" in owned_keys:
            bonus += 1.0  # Oops explicitly doubles this joker's proc rate
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
    candidate: dict | None = None,
    ante: int = 1,
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

    # Blueprint/Brainstorm copy synergy — data-driven from live roster state
    # rather than a hardcoded target list. Two cases:
    #   1. Candidate IS a copier: value scales with live best target + an
    #      ante-scaled prospective floor (buying Blueprint at ante 5 with an
    #      empty roster is betting the next shop has a scorer).
    #   2. Candidate is a potential target, copier already owned: apply the
    #      amplification ONLY if the candidate would beat the current best
    #      target — otherwise the copier is already pairing with something
    #      stronger and this buy doesn't add to the copy value.
    if candidate_key in _COPY_JOKERS:
        base = _copier_base_value(ante)
        live_target = _best_copy_target_value(owned_jokers)
        mult *= base + _copier_amplification(live_target)
    elif owned_keys & _COPY_JOKERS and candidate is not None:
        cand_score = _copy_target_score(candidate)
        # Strip tier offset for xmult, convert flat mult to xmult-equivalent
        if cand_score >= 100.0:
            cand_value = cand_score - 100.0
        elif cand_score > 0:
            cand_value = cand_score / 10.0
        else:
            cand_value = 0.0
        current_best = _best_copy_target_value(owned_jokers)
        if cand_value > current_best:
            # Candidate becomes the new best target — amplify by the delta
            # (not the absolute value — we already had the old target).
            mult *= 1.0 + _copier_amplification(cand_value - current_best)

    # --- Baseball Card synergy (rarity-based) ---
    uncommon_count = sum(
        1 for j in owned_jokers
        if j.get("value", {}).get("rarity") in (2, "Uncommon")
        and joker_key(j) != "j_baseball"
    )
    if candidate_key == "j_baseball":
        # Baseball scales with Uncommon count: more Uncommons = more x1.5 triggers
        mult *= 1.0 + uncommon_count * 0.3
    elif "j_baseball" in owned_keys and candidate is not None:
        # Uncommon candidates get a boost when Baseball is owned
        cand_rarity = candidate.get("value", {}).get("rarity")
        if cand_rarity in (2, "Uncommon"):
            mult *= 1.4

    # --- Trigger coherence (hand type overlap, pairwise) ---
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

    # --- Trigger coherence (rank overlap, pairwise) ---
    # Candidates that share target ranks with owned jokers compound on the
    # same deck work. E.g. Hack (2,3,4,5 retrigger) + Wee (+chips on 2) both
    # amplify when the deck is ace-and-low.
    cand_rank_entry = JOKER_RANK_AFFINITY.get(candidate_key)
    if cand_rank_entry and cand_rank_entry[1] > 0:
        candidate_ranks = set(cand_rank_entry[0])
        rank_allies = 0
        for j in owned_jokers:
            okey = joker_key(j)
            if okey == candidate_key:
                continue
            entry = JOKER_RANK_AFFINITY.get(okey)
            if not entry or entry[1] <= 0:
                continue
            if candidate_ranks & set(entry[0]):
                rank_allies += 1
        mult *= 1.0 + rank_allies * 0.12

    # --- Trigger coherence (suit overlap, pairwise) ---
    cand_suit_entry = JOKER_SUIT_AFFINITY.get(candidate_key)
    if cand_suit_entry:
        candidate_suit = cand_suit_entry[0]
        suit_allies = 0
        for j in owned_jokers:
            okey = joker_key(j)
            if okey == candidate_key:
                continue
            entry = JOKER_SUIT_AFFINITY.get(okey)
            if entry and entry[0] == candidate_suit:
                suit_allies += 1
        mult *= 1.0 + suit_allies * 0.15

    # --- Strategy alignment (candidate fits the broader plan) ---
    # This catches affinities the pairwise checks miss — archetype-driven
    # preferences, composite hands (e.g. strong Pair strategy implies Full
    # House is also valuable), and the deck profile's preferred ranks.
    # Normalized to avoid double-counting the pairwise bonuses above.
    if candidate_hands and strategy.preferred_hands:
        # Use strategy.hand_affinity to score each of the candidate's hands
        # against the roster's plan. Max weight in the affinity table is ~5,
        # so divide by 10 for a gentle nudge capped at ~1.5x.
        hand_align = max(
            strategy.hand_affinity(h) for h in candidate_hands
        ) if candidate_hands else 0.0
        mult *= 1.0 + min(0.5, hand_align / 10.0)

    if cand_rank_entry and cand_rank_entry[1] > 0 and strategy.preferred_ranks:
        rank_align = max(
            strategy.rank_affinity(r) for r in cand_rank_entry[0]
        ) if cand_rank_entry[0] else 0.0
        mult *= 1.0 + min(0.4, rank_align / 10.0)

    if cand_suit_entry and strategy.preferred_suits:
        suit_align = strategy.suit_affinity(cand_suit_entry[0])
        mult *= 1.0 + min(0.4, suit_align / 10.0)

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
    candidate_anchor_xmult: float = 1.0,
) -> float:
    """Context-dependent scaling: ante urgency + diminishing returns."""
    cat = _KEY_TO_CATEGORY.get(candidate_key)
    factor = 1.0

    # Ante urgency: xMult is always valuable; prefer it even early.
    # Scaling xmult (Madness, Hologram, etc.) only gets the late-game boost
    # if the anchor has actually accumulated — a fresh X1.0 Madness at
    # ante 8 has no runway, but an X2.0 Constellation at ante 4 is
    # effectively an immediate-fire X2 scorer. Threshold at X1.5 separates
    # "mostly future value" from "already firing hard."
    if cat == "xmult":
        is_fresh_scaling = (
            candidate_key in SCALING_XMULT_KEYS
            and candidate_anchor_xmult < 1.5
        )
        if ante <= 3:
            factor *= 1.15                                     # mild early-game preference
        elif is_fresh_scaling:
            factor *= 1.0                                      # no late-game boost for un-accumulated scalers
        else:
            factor *= min(1.0 + (ante - 3) * 0.4, 2.6)        # 1.4 @ ante 4, 2.6 @ ante 7+
    elif cat and ante >= 4:
        factor *= max(1.0 - (ante - 3) * 0.1, 0.6)            # 0.9 @ ante 4, 0.6 @ ante 7+

    # Diminishing returns within same category
    if cat:
        same_count = sum(
            1 for j in owned_jokers
            if _KEY_TO_CATEGORY.get(joker_key(j)) == cat
        )
        factor *= 1.0 / (1.0 + same_count * 0.25)

    # Cross-category saturation: once multiple scoring phases are covered,
    # the next scoring joker is marginal. Matches the tune signal that ~65%
    # of ante 1-2 joker buys after chip+mult coverage were more scorers.
    # Only applies to scoring jokers (chips/mult/xmult).
    if cat in ("chips", "mult", "xmult"):
        owned_cats = {
            _KEY_TO_CATEGORY.get(joker_key(j))
            for j in owned_jokers
        }
        covered = owned_cats & {"chips", "mult", "xmult"}
        if len(covered) >= 3:
            factor *= 0.4
        elif len(covered) == 2 and cat in covered:
            # Candidate adds a third of an already-covered category
            factor *= 0.6
        elif len(covered) == 2:
            # Candidate completes the trio (e.g. has chip+mult, buying xmult)
            # — still valuable, small damp only
            factor *= 0.9

    return factor


# ---------------------------------------------------------------------------
# Deck composition adjustments
# ---------------------------------------------------------------------------

def _deck_composition_adjustment(
    key: str,
    base_value: float,
    deck_profile: DeckProfile,
    strategy: Strategy | None,
    ante: int = 1,
) -> float:
    """Adjust base_value based on deck composition.

    Phase 3 moved Steel / Lucky / Glass / suit-enhancement boosts into the
    sim itself (scoring cards now carry density-planned enhancements; Steel
    & Glass candidates get their xmult projected from deck counts before the
    sim runs — see ``_project_deck_density_candidate`` and
    ``_plan_enhancements``). What stays here is only what the sim genuinely
    cannot express: Drivers License (activation gate, not per-card) and
    Blackboard (held-phase proc rate, driven by deck-wide S/C density).
    """
    # --- Drivers License: gate on enhanced card count ---
    if key == "j_drivers_license":
        enh_count = deck_profile.enhanced_card_count
        if enh_count < 12:
            base_value *= 0.1  # can't realistically activate
        elif enh_count < 16:
            base_value *= 0.5  # close but not there yet
        # >= 16: full value (already activated)

    # --- Blackboard: value scales with deck Spade/Club density ---
    # Activation requires ALL held cards to be S/C; higher S/C share in the
    # deck means proc rate climbs. Vanilla 26/52 is baseline (1.0×); 30+
    # S/C (Smeared, Midas Mask mid-game, Strength tarots toward dark suits,
    # spectral conversions) compounds into a substantial multiplier.
    if key == "j_blackboard":
        sc = deck_profile.suit_counts.get("S", 0) + deck_profile.suit_counts.get("C", 0)
        if sc >= 40:
            base_value *= 4.0
        elif sc >= 34:
            base_value *= 3.0
        elif sc >= 30:
            base_value *= 2.0
        elif sc >= 26:
            base_value *= 1.0
        else:
            base_value *= 0.4

    return base_value


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
    deck_profile: DeckProfile | None = None,
    unique_planets_used: int = 0,
    blind_name: str | None = None,
    live_stats: "LiveRunStats | None" = None,
    money: int = 0,
    discards_left: int = 0,
) -> float:
    """Unified joker valuation. Returns ~0.0 to ~15.0.

    Higher = more valuable to the current build.
    Used by both BuyJokersInShop and SellWeakJoker.
    """
    if strategy is None:
        strategy = compute_strategy(owned_jokers, hand_levels, deck_profile=deck_profile)

    cand_key = candidate.get("key", "") or joker_key(candidate)
    owned_key_set = {joker_key(j) for j in owned_jokers}
    # Phase 8: turn on Monte Carlo sampling only when a stochastic joker is
    # involved. The expected-value path stays the default for speed.
    mc_samples = _MC_DEFAULT_SAMPLES if (
        cand_key in _STOCHASTIC_KEYS or owned_key_set & _STOCHASTIC_KEYS
    ) else 0

    ctx = SimContext.build(
        candidate=candidate,
        owned_jokers=owned_jokers,
        hand_levels=hand_levels,
        strategy=strategy,
        ante=ante,
        joker_limit=joker_limit,
        deck_profile=deck_profile,
        unique_planets_used=unique_planets_used,
        blind_name=blind_name,
        monte_carlo_samples=mc_samples,
        live_stats=live_stats,
        money=money,
        discards_left=discards_left,
    )
    key = ctx.candidate_key
    owned_keys = set(ctx.owned_keys)

    # Determine hand types to simulate.
    #
    # The sim must represent what the bot will REALISTICALLY play — that's
    # always dominated by Pair and High Card, even when the roster's plan
    # points elsewhere. Strategy preferences layer ON TOP of that baseline
    # as bonus weight, rather than replacing it. Without this, a
    # deck-driven or archetype-driven preferred_hands (e.g. Pair 0.4, 3oK
    # 0.32, 4oK 0.24) dilutes Pair's sampling weight below the default 1.0
    # — the sim evaluates jokers primarily against rare high-baseline
    # hands where per-card marginal ratios are small, and under-values
    # jokers that fire on common hands.
    hand_weights: dict[str, float] = {"Pair": 1.0, "High Card": 0.5}
    if strategy.preferred_hands:
        # Normalize by top affinity so scale stays comparable. Top preferred
        # hand gets +1.0 bonus, others proportional. Preferences above HC/Pair
        # default become dominant; preferences below don't dominate
        # realistic play.
        top_score = max(score for _, score in strategy.preferred_hands)
        if top_score > 0:
            for h, score in strategy.preferred_hands[:5]:
                hand_weights[h] = hand_weights.get(h, 0.0) + score / top_score
    hand_types = [(h, w) for h, w in hand_weights.items() if w > 0]

    # Jokers that need 4+ scoring cards to trigger — bias the hand-type mix
    # so the condition can actually fire during valuation.
    if key == "j_flower_pot" or key == "j_seeing_double":
        if not any(h in ("Two Pair", "Flush", "Full House", "Four of a Kind", "Straight")
                   for h, _ in hand_types):
            hand_types = [("Two Pair", 1.0), ("Flush", 0.6)] if key == "j_flower_pot" \
                else [("Two Pair", 1.0), ("Pair", 0.6)]

    # Layer 1: scoring simulation or utility fallback
    if _has_scoring_effect(key):
        raw_delta = _scoring_delta(ctx, hand_types)
        # Flower Pot's sim assumes a proc (all 4 suits). In practice Two Pair /
        # Flush hands span 4 suits only occasionally; Smeared halves the
        # requirement to 2 red + 2 black which is much easier to hit.
        if key == "j_flower_pot":
            proc_rate = 0.70 if "j_smeared" in owned_keys else 0.30
            raw_delta *= proc_rate
        # Acrobat fires only on hands_left == 1 (finisher). The sim defaults
        # hands_left=1 so the X3 always fires. Discount to reflect that it
        # hits 1 of ~3.5 hands per round — but that 1 hand is the planned
        # finisher (biggest scoring hand, compounds with other late-hand
        # scalers like Dusk), so rate > pure 1/3.5.
        elif key == "j_acrobat":
            raw_delta *= 0.45
        # Card Sharp fires only on repeat-type plays (played_this_round > 0).
        # We synthesized played_this_round=1 above so the X3 fires in the
        # sim; discount for the fact that not every hand is a repeat. Bot
        # reliably commits to hand-type repeats via hand_sequencing, and the
        # repeat hands are the later/bigger ones, so rate > pure 2/3.5.
        elif key == "j_card_sharp":
            raw_delta *= 0.65
        # Mystic Summit fires at discards_left <= 0. Bot can deliberately
        # burn discards to reach the condition — usually on the last 1-2
        # hands of a round. Fires guaranteed once committed, so rate ~=
        # matches Acrobat's finisher weighting.
        elif key == "j_mystic_summit":
            raw_delta *= 0.45
        # Phase 3: rank-per-card jokers fire once per scored target rank. The
        # synthetic hand forces its target rank into a scoring slot (so the
        # effect fires at all), but how often that rank realistically shows up
        # across the ante scales with deck density. Factor=1.0 at vanilla
        # (4 copies baseline), linear out to 2.0 at 8+ copies, down to 0.25 at
        # 1 copy — matches Wee/Scholar density intuition from issue #35.
        # Rank density is captured directly by deck sampling under MC; the
        # post-hoc factor is only needed for the legacy synthetic-hand path.
        if not _MC_SAMPLING_ENABLED:
            raw_delta *= _rank_density_factor(ctx, key)
        coeff = _sim_coefficient(key)
        base_value = math.log2(1.0 + max(raw_delta, 0.0)) * coeff
    else:
        roi = utility_value.evaluate(
            key, ante=ante, deck_profile=deck_profile,
            owned_count=len(owned_jokers),
            unique_planets_used=unique_planets_used,
            strategy=strategy,
            owned_keys=ctx.owned_keys,
            owned_jokers=owned_jokers,
        )
        base_value = roi if roi is not None else UTILITY_VALUE.get(key, 0.0)
        base_value += _utility_synergy_bonus(key, owned_keys, strategy)

    # Riff-Raff: dynamic value based on available slots for spawns
    if key == "j_riff_raff":
        owned_count = len(owned_jokers)
        free_slots = max(0, joker_limit - owned_count)
        if free_slots <= 1:
            # No room for spawns (Riff-Raff itself takes the last slot)
            base_value = 0.0
        elif free_slots == 2:
            # Room for Riff-Raff + 1 spawn only
            base_value = 0.5
        else:
            # Room for Riff-Raff + 2 spawns — full value
            base_value = 2.0 if ante <= 3 else 1.0

    # Scaling-joker projection (Phase 4, issue #36): for every joker with a
    # ScalingProfile, predict end-of-run state as (live_anchor + projected
    # gain) and floor the sim with the same xmult × 5 / mult / 5 / chips / 50
    # conversion used for the live anchor alone. Live anchor comes from
    # ``ctx.lifetime`` (parsed from owned effect text); projection uses
    # per-joker trigger-rate models in ``scaling_projection``.
    if key in SCALING_REGISTRY:
        effect_text = candidate.get("value", {}).get("effect", "") or ""
        total_xmult = project_total_xmult(key, ctx)
        total_chips, total_mult = project_additive_total(key, ctx, effect_text)
        projected_end = {
            "xmult": total_xmult if total_xmult > 1.0 else None,
            "chips": total_chips if total_chips > 0 else None,
            "mult": total_mult if total_mult > 0 else None,
        }
        projected_floor = _dynamic_power(projected_end)
        base_value = max(base_value, projected_floor)

    # Hand-conditional xmult jokers read ~0 from the simulation when the
    # roster hasn't committed to their trigger hand. Correct for committed
    # builds but too harsh for cheap early blind-buys — apply a pivot floor
    # that fades by mid-game.
    if key in _HAND_CONDITIONAL_XMULT and base_value < PIVOT_FLOOR_THRESHOLD:
        target_hands = JOKER_HAND_AFFINITY.get(key, ([], 0))[0]
        aligned = any(strategy.hand_affinity(h) > 0 for h in target_hands)
        if not aligned:
            floor = max(0.0, PIVOT_FLOOR_A1 - (ante - 1) * PIVOT_FLOOR_DECAY)
            base_value = max(base_value, floor)

    # Madness fodder: extra bodies dilute Madness's random-eat, protecting real
    # scalers. No bonus when slots are full (no room for fodder) or when the
    # candidate is Madness itself.
    if (
        key != "j_madness"
        and "j_madness" in owned_keys
        and len(owned_jokers) < joker_limit
    ):
        base_value += 2.0 / max(1, len(owned_jokers) - 1)

    # Deck composition adjustment — boost/gate jokers based on deck contents
    if deck_profile is not None:
        base_value = _deck_composition_adjustment(key, base_value, deck_profile, strategy, ante)

    # Layer 2: synergy
    synergy = _synergy_multiplier(key, owned_keys, strategy, owned_jokers, candidate, ante)

    # Layer 3: context. Pass live xmult anchor for scaling xmult candidates so
    # the ante-urgency boost applies only when the joker is already firing.
    candidate_anchor = 1.0
    if ctx.lifetime is not None and key in SCALING_XMULT_KEYS:
        # Re-parse anchor from the candidate's own effect text (covers
        # buy-in-shop case where the candidate isn't in ctx.lifetime).
        from balatro_bot.domain.policy.sim_context import LifetimeState
        candidate_lt = LifetimeState.from_owned([candidate])
        field_name = {
            "j_madness": "madness_xmult",
            "j_hologram": "hologram_xmult",
            "j_caino": "caino_xmult",
            "j_vampire": "vampire_xmult",
            "j_obelisk": "obelisk_xmult",
            "j_yorick": "yorick_xmult",
            "j_campfire": "campfire_xmult",
            "j_constellation": "constellation_xmult",
            "j_throwback": "throwback_xmult",
            "j_hit_the_road": "hit_the_road_xmult",
            "j_lucky_cat": "lucky_cat_xmult",
            "j_glass": "glass_xmult",
        }.get(key)
        if field_name:
            candidate_anchor = getattr(candidate_lt, field_name, 1.0)
    context = _context_scale(key, owned_jokers, ante, candidate_anchor)

    # Layer 4 (Phase 7): boss-blind adjustment. In-round uses the active boss;
    # shop phase blends across the upcoming-boss pool. Note: if the caller
    # passed a blind_name that isn't in _BOSS_TEMPLATES (non-templated boss
    # like Verdant Leaf / Cerulean Bell / Crimson Heart), from_name() returns
    # None and ctx.boss stays unset — we fall back to the shop blend even
    # mid-round. That's intentional: the blend is a reasonable prior when we
    # have no specific adjustment logic for the active boss.
    if ctx.boss is not None:
        boss_mult = boss_multiplier(key, ctx.boss)
    else:
        boss_mult = shop_blended_multiplier(key)

    return base_value * synergy * context * boss_mult + _edition_bonus(candidate)


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
