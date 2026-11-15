"""Joker effect registry — merges simple (data-driven) and complex (hand-written) effects."""

from __future__ import annotations

from typing import Callable, TYPE_CHECKING

from balatro_bot.cards import _modifier
from balatro_bot.domain.models.joker import Joker
from balatro_bot.joker_effects.context import ScoreContext, _noop
from balatro_bot.joker_effects.roster import Roster
from balatro_bot.joker_effects.simple import SIMPLE_EFFECTS
from balatro_bot.joker_effects.complex import COMPLEX_EFFECTS

if TYPE_CHECKING:
    from typing import Any

    JokerLike = Joker | dict[str, Any]


# Merge simple and complex effects. Complex takes precedence if both define the same key.
JOKER_EFFECTS: dict[str, Callable[[ScoreContext, JokerLike], None]] = {}
JOKER_EFFECTS.update(SIMPLE_EFFECTS)
JOKER_EFFECTS.update(COMPLEX_EFFECTS)

# --- Retrigger jokers (scoring handled in retrigger_count) ---
for key in ("j_hack", "j_dusk", "j_sock_and_buskin", "j_hanging_chad", "j_selzer", "j_mime"):
    JOKER_EFFECTS[key] = _noop

# --- Per-card jokers handled in _apply_card_scoring (domain/scoring/estimate.py) ---
# Hiker: +5 chips per card per trigger — handled as "all_chips" in per-card loop
JOKER_EFFECTS["j_hiker"] = _noop

# --- Held-in-hand phase jokers (applied in _apply_card_scoring, before joker effects) ---
# Baron: x1.5 per held King — fires alongside Steel in held-in-hand phase
JOKER_EFFECTS["j_baron"] = _noop
# Shoot the Moon: +13 mult per held Queen — fires in held-in-hand phase
JOKER_EFFECTS["j_shoot_the_moon"] = _noop
# Raised Fist: +2×(lowest held rank chips) — fires in held-in-hand phase, not joker phase
JOKER_EFFECTS["j_raised_fist"] = _noop

# --- Per-card jokers (applied per trigger in _apply_card_scoring, before joker effects) ---
# These fire "when scored" interleaved with Glass xmult / editions / retriggers.
for key in (
    "j_fibonacci", "j_even_steven", "j_odd_todd", "j_scholar",
    "j_smiley", "j_scary_face", "j_walkie_talkie",
    "j_photograph", "j_triboulet", "j_ancient",
    "j_arrowhead", "j_onyx_agate", "j_bloodstone", "j_idol",
    "j_greedy_joker", "j_lusty_joker", "j_wrathful_joker", "j_gluttenous_joker",
):
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


def _is_uncommon(joker: JokerLike) -> bool:
    """Check if a joker is Uncommon rarity (API sends int or string)."""
    if isinstance(joker, Joker):
        return joker.value.rarity in (2, "Uncommon")
    rarity = joker.get("value", {}).get("rarity")
    return rarity in (2, "Uncommon")


def _effect_plan(lineup: Roster) -> tuple:
    """Per active joker: (joker, effect, edition +chips, edition +mult, edition xmult, baseball).

    Edition numbers are None when the edition adds nothing: Foil gives chips and
    Holo gives mult before the joker's effect, Polychrome multiplies after it
    (Negative only adds a slot). effect is None for jokers that score nothing in
    this phase. baseball marks Uncommon jokers when a Baseball Card is in effect.
    """
    plan = lineup.derived.get("effect_plan")
    if plan is None:
        baseball_xm = lineup.baseball_xmults
        steps = []
        for key, joker in lineup.active:
            mod = _modifier(joker)
            edition = mod.get("edition", "")
            pre_chips = mod.get("edition_chips", 50) if edition == "FOIL" else None
            pre_mult = mod.get("edition_mult", 10) if edition in ("HOLO", "HOLOGRAPHIC") else None
            post_xmult = None
            if edition == "POLYCHROME":
                post_xmult = mod.get("edition_x_mult", 1.5)
            elif mod.get("edition_x_mult") and edition not in ("FOIL", "HOLO", "HOLOGRAPHIC", "NEGATIVE"):
                post_xmult = mod["edition_x_mult"]
            effect = JOKER_EFFECTS.get(key)
            if effect is _noop:
                effect = None
            baseball = bool(baseball_xm) and key != "j_baseball" and _is_uncommon(joker)
            steps.append((joker, effect, pre_chips, pre_mult, post_xmult, baseball))
        plan = lineup.derived["effect_plan"] = tuple(steps)
    return plan


def _apply_step(ctx: ScoreContext, step: tuple, baseball_xms: tuple[float, ...]) -> None:
    """One joker's turn: pre-edition -> joker effect -> post-edition -> Baseball Card."""
    joker, effect, pre_chips, pre_mult, post_xmult, baseball = step
    if pre_chips is not None:
        ctx.chips += pre_chips
    elif pre_mult is not None:
        ctx.mult += pre_mult
    if effect is not None:
        effect(ctx, joker)
    if post_xmult is not None:
        ctx.mult *= post_xmult
    if baseball:
        for xmult in baseball_xms:
            ctx.mult *= xmult


def apply_joker_effects(ctx: ScoreContext) -> None:
    """Apply all owned joker effects to the scoring context, in order."""
    baseball_xm = ctx.roster.baseball_xmults
    for step in _effect_plan(ctx.roster):
        _apply_step(ctx, step, baseball_xm)


def apply_joker_effects_detailed(ctx: ScoreContext) -> list[tuple[str, float, float, float]]:
    """Like apply_joker_effects, but returns per-joker (label, delta_chips, delta_mult, xmult_factor).

    xmult_factor is the multiplicative ratio (e.g. 2.0 means mult was doubled).
    A value of 1.0 means no xmult was applied (change was additive only).
    """
    baseball_xm = ctx.roster.baseball_xmults
    contributions: list[tuple[str, float, float, float]] = []
    for (key, _), step in zip(ctx.roster.active, _effect_plan(ctx.roster)):
        joker = step[0]
        label = joker.label if isinstance(joker, Joker) else joker.get("label", key)
        pre_chips, pre_mult = ctx.chips, ctx.mult
        _apply_step(ctx, step, baseball_xm)
        dc = ctx.chips - pre_chips
        dm = ctx.mult - pre_mult
        # Detect xmult: if mult changed multiplicatively (ratio != 1 + additive)
        if pre_mult > 0 and dm != 0:
            xmult = ctx.mult / pre_mult
        else:
            xmult = 1.0
        contributions.append((label, dc, dm, xmult))
    return contributions
