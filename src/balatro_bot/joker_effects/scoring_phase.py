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

# Xmult jokers that fire while each card scores or is held, before the joker
# phase, so their own slot barely matters. They still sort with the xmult group.
PER_CARD_XMULT = frozenset({
    "j_photograph", "j_triboulet", "j_ancient", "j_bloodstone", "j_baron", "j_idol",
})

# One-line jokers take their phase from the kernel's table (kernel/core/src/jokers.rs).
_KERNEL_PHASE = {"chips": PHASE_CHIPS, "mult": PHASE_MULT, "xmult": PHASE_XMULT}

# ---------------------------------------------------------------------------
# Jokers with their own logic, or that score outside the joker phase — manually classified
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
    "j_blue_joker": PHASE_CHIPS,
    "j_hiker": PHASE_CHIPS,      # chips go onto each card as it scores

    # +mult (additive)
    "j_greedy_joker": PHASE_MULT,
    "j_lusty_joker": PHASE_MULT,
    "j_wrathful_joker": PHASE_MULT,
    "j_gluttenous_joker": PHASE_MULT,
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

    "j_photograph": PHASE_XMULT,
    "j_triboulet": PHASE_XMULT,
    "j_ancient": PHASE_XMULT,

    "j_lucky_cat": PHASE_XMULT,
    "j_vampire": PHASE_XMULT,
    "j_obelisk": PHASE_XMULT,
    "j_steel_joker": PHASE_XMULT,
    "j_ramen": PHASE_XMULT,
    "j_yorick": PHASE_XMULT,

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


def _phase_map() -> dict[str, int]:
    global _PHASE_MAP
    if _PHASE_MAP is None:
        _PHASE_MAP = {}
        from balatro_bot.domain.scoring.kernel import joker_phases
        _PHASE_MAP.update(
            (k, _KERNEL_PHASE[phase]) for k, phase in joker_phases().items() if phase in _KERNEL_PHASE)
        _PHASE_MAP.update(_COMPLEX_PHASES)
        for k in _RETRIGGER_KEYS:
            _PHASE_MAP[k] = PHASE_RETRIGGER
        for k in _NOOP_KEYS:
            _PHASE_MAP[k] = PHASE_NOOP
    return _PHASE_MAP


def get_joker_phase(key: str) -> int:
    """Return the scoring phase for a joker key."""
    return _phase_map().get(key, PHASE_MULT)  # unknown jokers default to +mult (safe middle)


_ROLE = {PHASE_CHIPS: "chips", PHASE_MULT: "mult", PHASE_XMULT: "xmult"}


def joker_role(key: str) -> str:
    """``"chips"``, ``"mult"`` or ``"xmult"`` for a joker that scores in that phase, else
    ``"other"`` (retriggers, economy and utility, and jokers this table does not know)."""
    return _ROLE.get(_phase_map().get(key), "other")


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
