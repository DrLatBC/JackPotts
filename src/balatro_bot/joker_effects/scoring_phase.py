"""Joker scoring phase classification for optimal roster ordering.

In Balatro, joker effects fire left-to-right. Additive effects (+chips, +mult)
should be placed LEFT of multiplicative effects (×mult) to maximize scores.

This module classifies each joker's primary scoring phase so the bot can
arrange jokers in optimal order.
"""

from __future__ import annotations

# Phase constants — sort order matches optimal left-to-right placement
PHASE_NOOP = 0       # No scoring effect at all
PHASE_RETRIGGER = 0  # No scoring effect in joker phase
PHASE_CHIPS = 1      # Adds flat chips
PHASE_MULT = 2       # Adds flat mult
PHASE_XMULT = 3      # Multiplies mult
PHASE_PER_CARD_XMULT = 3  # Per-card ×mult (Photograph, Triboulet, Ancient) — sort with xmult

# ---------------------------------------------------------------------------
# Auto-classify from simple effects table dispatcher names
# ---------------------------------------------------------------------------
_DISPATCHER_PHASE = {
    "flat_mult": PHASE_MULT,
    "parsed_mult": PHASE_MULT,
    "hand_mult": PHASE_MULT,
    "suit_mult": PHASE_MULT,
    "parsed_chips": PHASE_CHIPS,
    "hand_chips": PHASE_CHIPS,
    "hand_parsed_chips": PHASE_CHIPS,
    "parsed_xmult": PHASE_XMULT,
    "hand_xmult": PHASE_XMULT,
}

def _build_from_simple_table() -> dict[str, int]:
    """Derive phase from SIMPLE_EFFECTS_TABLE dispatcher names."""
    from balatro_bot.joker_effects.simple import SIMPLE_EFFECTS_TABLE
    result = {}
    for key, dispatcher_name, _params in SIMPLE_EFFECTS_TABLE:
        phase = _DISPATCHER_PHASE.get(dispatcher_name, PHASE_NOOP)
        result[key] = phase
    return result

# ---------------------------------------------------------------------------
# Complex effects — manually classified
# ---------------------------------------------------------------------------
_COMPLEX_PHASES: dict[str, int] = {
    # +chips (additive)
    "j_runner": PHASE_CHIPS,
    "j_square": PHASE_CHIPS,
    "j_scary_face": PHASE_CHIPS,
    "j_odd_todd": PHASE_CHIPS,
    "j_scholar": PHASE_CHIPS,      # +chips AND +mult — chips is primary
    "j_walkie_talkie": PHASE_CHIPS, # +chips AND +mult — chips is primary
    "j_arrowhead": PHASE_CHIPS,
    "j_bull": PHASE_CHIPS,
    "j_stuntman": PHASE_CHIPS,
    "j_wee": PHASE_CHIPS,

    # +mult (additive)
    "j_half": PHASE_MULT,
    "j_stencil": PHASE_MULT,       # technically xmult-like but reads as mult
    "j_banner": PHASE_MULT,
    "j_mystic_summit": PHASE_MULT,
    "j_loyalty_card": PHASE_MULT,   # xmult when charged, but classified as mult phase
    "j_misprint": PHASE_MULT,
    "j_raised_fist": PHASE_MULT,
    "j_fibonacci": PHASE_MULT,
    "j_abstract": PHASE_MULT,
    "j_even_steven": PHASE_MULT,
    "j_smiley": PHASE_MULT,
    "j_supernova": PHASE_MULT,
    "j_trousers": PHASE_MULT,
    "j_green_joker": PHASE_MULT,
    "j_ride_the_bus": PHASE_MULT,
    "j_shoot_the_moon": PHASE_MULT,
    "j_bootstraps": PHASE_MULT,
    "j_swashbuckler": PHASE_MULT,
    "j_onyx_agate": PHASE_MULT,

    # ×mult (multiplicative)
    "j_blackboard": PHASE_XMULT,
    "j_baron": PHASE_XMULT,
    "j_acrobat": PHASE_XMULT,
    "j_card_sharp": PHASE_XMULT,
    "j_seeing_double": PHASE_XMULT,
    "j_flower_pot": PHASE_XMULT,
    "j_drivers_license": PHASE_XMULT,
    "j_bloodstone": PHASE_XMULT,
    "j_baseball": PHASE_XMULT,
    "j_idol": PHASE_XMULT,
    "j_loyalty_card": PHASE_XMULT,

    # Per-card xmult (fires during card scoring, but sort with ×mult group
    # so additive jokers fire first in the joker phase and editions are positioned correctly)
    "j_photograph": PHASE_PER_CARD_XMULT,
    "j_triboulet": PHASE_PER_CARD_XMULT,
    "j_ancient": PHASE_PER_CARD_XMULT,

    # Position-dependent (special handling, default to noop phase for sorting)
    "j_blueprint": PHASE_NOOP,
    "j_brainstorm": PHASE_NOOP,
}

# ---------------------------------------------------------------------------
# Retrigger jokers
# ---------------------------------------------------------------------------
_RETRIGGER_KEYS = frozenset({
    "j_hack", "j_dusk", "j_sock_and_buskin", "j_hanging_chad", "j_selzer", "j_mime",
})

# ---------------------------------------------------------------------------
# Economy / utility jokers (noop)
# ---------------------------------------------------------------------------
_NOOP_KEYS = frozenset({
    "j_credit_card", "j_chaos", "j_delayed_grat", "j_business", "j_egg",
    "j_faceless", "j_todo_list", "j_cloud_9", "j_rocket", "j_gift",
    "j_reserved_parking", "j_mail", "j_to_the_moon", "j_golden",
    "j_trading", "j_ticket", "j_rough_gem", "j_matador", "j_satellite",
    "j_astronomer",
    "j_four_fingers", "j_marble", "j_8_ball", "j_space", "j_burglar",
    "j_dna", "j_splash", "j_sixth_sense", "j_superposition", "j_shortcut",
    "j_seance", "j_riff_raff", "j_vagabond", "j_midas_mask", "j_luchador",
    "j_turtle_bean", "j_hallucination", "j_juggler", "j_drunkard",
    "j_diet_cola", "j_mr_bones", "j_pareidolia", "j_troubadour",
    "j_certificate", "j_smeared", "j_ring_master", "j_merry_andy",
    "j_oops", "j_invisible", "j_burnt", "j_cartomancer", "j_chicot",
    "j_perkeo",
})

# ---------------------------------------------------------------------------
# Merged classification — built once at import time
# ---------------------------------------------------------------------------
_PHASE_MAP: dict[str, int] | None = None

def get_joker_phase(key: str) -> int:
    """Return the scoring phase for a joker key."""
    global _PHASE_MAP
    if _PHASE_MAP is None:
        _PHASE_MAP = {}
        _PHASE_MAP.update(_build_from_simple_table())
        _PHASE_MAP.update(_COMPLEX_PHASES)
        for k in _RETRIGGER_KEYS:
            _PHASE_MAP[k] = PHASE_RETRIGGER
        for k in _NOOP_KEYS:
            _PHASE_MAP[k] = PHASE_NOOP
    return _PHASE_MAP.get(key, PHASE_MULT)  # unknown jokers default to +mult (safe middle)


def reorder_for_scoring(jokers: list) -> list:
    """Return *jokers* in optimal scoring-phase order for the sim.

    Applies the same chips→mult→xmult sort the live bot runs via
    ``ReorderJokersForScoring``, but without Ceremonial Dagger fodder logic
    (which is a live-play concern, not a scoring-sim concern).

    Blueprint/Brainstorm copy-adjacency constraints are preserved:
    - Blueprint sits left of the best xmult target (or last if none).
    - Brainstorm sits last with a non-noop, copy-compatible joker at index 0.
    """
    from balatro_bot.cards import joker_key
    from balatro_bot.scaling import BLUEPRINT_INCOMPATIBLE

    if len(jokers) < 2:
        return list(jokers)

    blueprint_idx: int | None = None
    brainstorm_idx: int | None = None
    for i, j in enumerate(jokers):
        k = joker_key(j)
        if k == "j_blueprint":
            blueprint_idx = i
        elif k == "j_brainstorm":
            brainstorm_idx = i

    excluded = {blueprint_idx, brainstorm_idx} - {None}
    sortable = []
    for i, j in enumerate(jokers):
        if i in excluded:
            continue
        k = joker_key(j)
        phase = get_joker_phase(k)
        ed_phase = get_joker_edition_phase(j)
        sortable.append((i, phase, ed_phase))
    sortable.sort(key=lambda x: (x[1], x[2], x[0]))
    order = [i for i, _, _ in sortable]

    if blueprint_idx is not None:
        best_copy_pos = None
        for pos, idx in enumerate(order):
            k = joker_key(jokers[idx])
            if k in BLUEPRINT_INCOMPATIBLE:
                continue
            phase = get_joker_phase(k)
            if phase == PHASE_XMULT:
                best_copy_pos = pos
            elif phase == PHASE_MULT and best_copy_pos is None:
                best_copy_pos = pos
        if best_copy_pos is not None:
            order.insert(best_copy_pos, blueprint_idx)
        else:
            order.append(blueprint_idx)

    if brainstorm_idx is not None:
        order.append(brainstorm_idx)
        if order:
            first_key = joker_key(jokers[order[0]])
            if get_joker_phase(first_key) == PHASE_NOOP or first_key in BLUEPRINT_INCOMPATIBLE:
                for pos in range(1, len(order)):
                    sk = joker_key(jokers[order[pos]])
                    if get_joker_phase(sk) != PHASE_NOOP and sk not in BLUEPRINT_INCOMPATIBLE:
                        order[0], order[pos] = order[pos], order[0]
                        break

    if order == list(range(len(jokers))):
        return list(jokers)
    return [jokers[i] for i in order]


def get_joker_edition_phase(joker) -> int:
    """Return the scoring phase implied by a joker's edition (if any).

    Foil → chips phase, Holo → mult phase, Polychrome → xmult phase.
    No edition → PHASE_NOOP (no secondary phase).
    """
    from balatro_bot.domain.models.joker import Joker
    if isinstance(joker, Joker):
        edition = joker.modifier.edition or ""
    else:
        mod = joker.get("modifier", joker.get("value", {}).get("modifier", {}))
        if isinstance(mod, list):
            return PHASE_NOOP
        edition = mod.get("edition", "")
    if edition == "FOIL":
        return PHASE_CHIPS
    if edition in ("HOLO", "HOLOGRAPHIC"):
        return PHASE_MULT
    if edition == "POLYCHROME":
        return PHASE_XMULT
    return PHASE_NOOP
