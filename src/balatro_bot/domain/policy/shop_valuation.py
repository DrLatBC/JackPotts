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
from balatro_bot.domain.models.card import Card, CardModifier, CardValue
from balatro_bot.domain.policy import utility_value
from balatro_bot.domain.policy.scaling_projection import (
    project_additive_total,
    project_total_xmult,
)
from balatro_bot.domain.policy.sim_context import SimContext
from balatro_bot.domain.scoring.estimate import score_hand
from balatro_bot.joker_effects import JOKER_EFFECTS, _noop
from balatro_bot.scaling import BLUEPRINT_INCOMPATIBLE, SCALING_REGISTRY
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
    total_weight = sum(w for _, w in hand_types)
    if total_weight <= 0:
        return 0.0

    candidate = _project_deck_density_candidate(ctx)
    owned_jokers = ctx.owned_jokers
    hand_levels = ctx.hand_levels
    joker_limit = ctx.joker_limit
    strategy = ctx.strategy

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

        baseline = score_hand(
            hand_name, scoring_cards, hand_levels,
            jokers=baseline_jokers, played_cards=played_cards,
            held_cards=held_cards,
            joker_limit=joker_limit,
            ancient_suit=ancient_suit, idol_rank=idol_rank, idol_suit=idol_suit,
        )
        with_candidate = score_hand(
            hand_name, scoring_cards, hand_levels,
            jokers=_place_candidate(baseline_jokers), played_cards=played_cards,
            held_cards=held_cards,
            joker_limit=joker_limit,
            ancient_suit=ancient_suit, idol_rank=idol_rank, idol_suit=idol_suit,
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
_PROBABILITY_JOKERS = frozenset({
    "j_oops", "j_lucky_cat", "j_bloodstone", "j_8_ball",
    "j_space", "j_sixth_sense",
})
_HAND_CONDITIONAL_XMULT = frozenset({
    "j_duo", "j_trio", "j_tribe", "j_family", "j_order",
})
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

    # Ante urgency: xMult is always valuable; prefer it even early
    if cat == "xmult":
        if ante <= 3:
            factor *= 1.15                                     # mild early-game preference
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
) -> float:
    """Unified joker valuation. Returns ~0.0 to ~15.0.

    Higher = more valuable to the current build.
    Used by both BuyJokersInShop and SellWeakJoker.
    """
    if strategy is None:
        strategy = compute_strategy(owned_jokers, hand_levels)

    ctx = SimContext.build(
        candidate=candidate,
        owned_jokers=owned_jokers,
        hand_levels=hand_levels,
        strategy=strategy,
        ante=ante,
        joker_limit=joker_limit,
        deck_profile=deck_profile,
        unique_planets_used=unique_planets_used,
    )
    key = ctx.candidate_key
    owned_keys = set(ctx.owned_keys)

    # Determine hand types to simulate
    if strategy.preferred_hands:
        hand_types = strategy.preferred_hands[:3]
    else:
        hand_types = [("Pair", 1.0), ("High Card", 0.5)]

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
        # Phase 3: rank-per-card jokers fire once per scored target rank. The
        # synthetic hand forces its target rank into a scoring slot (so the
        # effect fires at all), but how often that rank realistically shows up
        # across the ante scales with deck density. Factor=1.0 at vanilla
        # (4 copies baseline), linear out to 2.0 at 8+ copies, down to 0.25 at
        # 1 copy — matches Wee/Scholar density intuition from issue #35.
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
    synergy = _synergy_multiplier(key, owned_keys, strategy, owned_jokers, candidate)

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
