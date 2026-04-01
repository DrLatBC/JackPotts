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

# --- Post-scoring jokers (effect already reflected in card data) ---
# Hiker: +5 chips permanently stamped onto cards after scoring; already in perma_bonus
JOKER_EFFECTS["j_hiker"] = _noop

# --- Held-in-hand phase jokers (applied in _apply_card_scoring, before joker effects) ---
# Baron: x1.5 per held King — fires alongside Steel in held-in-hand phase
JOKER_EFFECTS["j_baron"] = _noop

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


def _joker_modifier(joker: dict) -> dict:
    """Return the modifier dict for a joker, handling [] for empty."""
    m = joker.get("modifier", {})
    return m if isinstance(m, dict) else {}


def _apply_joker_edition_pre(ctx: ScoreContext, joker: dict) -> None:
    """Apply pre-joker edition effects: Foil (+chips), HOLO (+mult)."""
    mod = _joker_modifier(joker)
    edition = mod.get("edition", "")
    if edition == "FOIL":
        ctx.chips += mod.get("edition_chips", 50)
    elif edition in ("HOLO", "HOLOGRAPHIC"):
        ctx.mult += mod.get("edition_mult", 10)


def _apply_joker_edition_post(ctx: ScoreContext, joker: dict) -> None:
    """Apply post-joker edition effects: Polychrome (xmult).
    Negative has no scoring effect (just +1 slot, already in joker_limit)."""
    mod = _joker_modifier(joker)
    edition = mod.get("edition", "")
    if edition == "POLYCHROME":
        ctx.mult *= mod.get("edition_x_mult", 1.5)
    elif mod.get("edition_x_mult") and edition not in ("FOIL", "HOLO", "HOLOGRAPHIC", "NEGATIVE"):
        ctx.mult *= mod["edition_x_mult"]


def _is_uncommon(joker: dict) -> bool:
    """Check if a joker is Uncommon rarity (API sends int or string)."""
    rarity = joker.get("value", {}).get("rarity")
    return rarity in (2, "Uncommon")


def _get_baseball_xmult(ctx: ScoreContext) -> float:
    """Return Baseball Card's xmult if owned, else 0."""
    for j in ctx.jokers:
        if j.get("key") == "j_baseball":
            from balatro_bot.joker_effects.parsers import _ability
            return _ability(j).get("extra", 1.5)
    return 0.0


def apply_joker_effects(ctx: ScoreContext) -> None:
    """Apply all owned joker effects to the scoring context, in order.

    Per joker, the game applies: pre-edition → joker effect → post-edition.
    Baseball Card: after each Uncommon joker fires, apply an additional xMult.
    """
    baseball_xm = _get_baseball_xmult(ctx)

    for joker in ctx.jokers:
        _apply_joker_edition_pre(ctx, joker)
        key = joker.get("key", "")
        effect = JOKER_EFFECTS.get(key)
        if effect is not None:
            effect(ctx, joker)
        _apply_joker_edition_post(ctx, joker)
        # Baseball Card: x1.5 after each Uncommon joker's effect
        if baseball_xm and key != "j_baseball" and _is_uncommon(joker):
            ctx.mult *= baseball_xm


def apply_joker_effects_detailed(ctx: ScoreContext) -> list[tuple[str, float, float, float]]:
    """Like apply_joker_effects, but returns per-joker (label, delta_chips, delta_mult, xmult_factor).

    xmult_factor is the multiplicative ratio (e.g. 2.0 means mult was doubled).
    A value of 1.0 means no xmult was applied (change was additive only).
    """
    baseball_xm = _get_baseball_xmult(ctx)
    contributions: list[tuple[str, float, float, float]] = []
    for joker in ctx.jokers:
        key = joker.get("key", "")
        label = joker.get("label", key)
        pre_chips, pre_mult = ctx.chips, ctx.mult
        _apply_joker_edition_pre(ctx, joker)
        effect = JOKER_EFFECTS.get(key)
        if effect is not None:
            effect(ctx, joker)
        _apply_joker_edition_post(ctx, joker)
        if baseball_xm and key != "j_baseball" and _is_uncommon(joker):
            ctx.mult *= baseball_xm
        dc = ctx.chips - pre_chips
        dm = ctx.mult - pre_mult
        # Detect xmult: if mult changed multiplicatively (ratio != 1 + additive)
        if pre_mult > 0 and dm != 0:
            xmult = ctx.mult / pre_mult
        else:
            xmult = 1.0
        contributions.append((label, dc, dm, xmult))
    return contributions
