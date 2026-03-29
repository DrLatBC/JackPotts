"""Joker effect registry — merges simple (data-driven) and complex (hand-written) effects."""

from __future__ import annotations

from typing import Callable

from balatro_bot.joker_effects.context import ScoreContext, _noop
from balatro_bot.joker_effects.simple import SIMPLE_EFFECTS
from balatro_bot.joker_effects.complex import COMPLEX_EFFECTS


# Merge simple and complex effects. Complex takes precedence if both define the same key.
JOKER_EFFECTS: dict[str, Callable[[ScoreContext, dict], None]] = {}
JOKER_EFFECTS.update(SIMPLE_EFFECTS)
JOKER_EFFECTS.update(COMPLEX_EFFECTS)

# --- Retrigger jokers (scoring handled in retrigger_count) ---
for key in ("j_hack", "j_dusk", "j_sock_and_buskin", "j_hanging_chad", "j_seltzer", "j_mime"):
    JOKER_EFFECTS[key] = _noop

# --- Economy jokers (no scoring effect, but recognized as "known") ---
for key in (
    "j_credit_card", "j_chaos", "j_delayed_grat", "j_business", "j_egg",
    "j_faceless", "j_todo_list", "j_cloud_9", "j_rocket", "j_gift",
    "j_reserved_parking", "j_mail", "j_to_the_moon", "j_golden",
    "j_trading", "j_ticket", "j_rough_gem", "j_matador", "j_satellite",
    "j_astronomer",
):
    JOKER_EFFECTS[key] = _noop

# --- Utility jokers ---
for key in (
    "j_four_fingers", "j_marble", "j_8_ball", "j_space", "j_burglar",
    "j_dna", "j_splash", "j_sixth_sense", "j_superposition", "j_shortcut",
    "j_seance", "j_riff_raff", "j_vagabond", "j_midas_mask", "j_luchador",
    "j_turtle_bean", "j_hallucination", "j_juggler", "j_drunkard",
    "j_diet_cola", "j_mr_bones", "j_pareidolia", "j_troubadour",
    "j_certificate", "j_smeared", "j_ring_master", "j_merry_andy",
    "j_oops", "j_invisible", "j_burnt", "j_cartomancer", "j_chicot",
    "j_perkeo",
):
    JOKER_EFFECTS[key] = _noop


def apply_joker_effects(ctx: ScoreContext) -> None:
    """Apply all owned joker effects to the scoring context, in order."""
    for joker in ctx.jokers:
        key = joker.get("key", "")
        effect = JOKER_EFFECTS.get(key)
        if effect is not None:
            effect(ctx, joker)


def apply_joker_effects_detailed(ctx: ScoreContext) -> list[tuple[str, float, float]]:
    """Like apply_joker_effects, but returns per-joker (label, delta_chips, delta_mult)."""
    contributions = []
    for joker in ctx.jokers:
        key = joker.get("key", "")
        label = joker.get("label", key)
        effect = JOKER_EFFECTS.get(key)
        if effect is not None:
            pre_chips, pre_mult = ctx.chips, ctx.mult
            effect(ctx, joker)
            contributions.append((
                label,
                ctx.chips - pre_chips,
                ctx.mult - pre_mult,
            ))
    return contributions
