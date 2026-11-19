"""Projection of each scaling joker's end-of-run state.

``project_end_state`` returns the xmult, chips and mult a scaling joker is
expected to reach by the end of the run: its anchor (the live value from
``joker_effects/live_values``; X1.0 or 0 when not owned) plus the gain its
trigger is expected to add over the runway left. The gain per trigger, and the
trigger rate of chip/mult scalers, come from the joker's ``ScalingProfile``;
the models here supply how often each xmult trigger fires and how much of the
gain is realized.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from balatro_bot.joker_effects.parsers import current_value
from balatro_bot.scaling import SCALING_REGISTRY

if TYPE_CHECKING:
    from balatro_bot.domain.policy.sim_context import LifetimeState, SimContext


# Rough blind-count model. Balatro has 3 blinds per ante (Small/Big/Boss).
# ``ante`` here is the current ante (1-indexed); ante 8 is the final ante.
def _blinds_remaining(ante: int) -> int:
    return max(0, 3 * max(0, 9 - ante))


def _rounds_remaining(ante: int) -> int:
    # 1 round == 1 blind in Balatro; kept separate for readability.
    return _blinds_remaining(ante)


def _ante_remaining(ante: int) -> int:
    return max(0, 9 - ante)


def _gain(key: str) -> float:
    return float(SCALING_REGISTRY[key].gain_per)


# Per-joker xmult models. Each returns the gain over the anchor.

def _project_madness_gain(ctx: "SimContext", lt: "LifetimeState") -> float:
    # One trigger per blind select, but Madness destroys a random joker each
    # blind: halve the naive projection for the roster power it costs.
    return _gain("j_madness") * _blinds_remaining(ctx.ante) * 0.5


def _project_throwback_gain(ctx: "SimContext", lt: "LifetimeState") -> float:
    # Bot skip policy averages ~2 blind skips per run. Scale by remaining runway.
    remaining_fraction = _ante_remaining(ctx.ante) / 8.0
    return 2.0 * _gain("j_throwback") * remaining_fraction


def _project_caino_gain(ctx: "SimContext", lt: "LifetimeState") -> float:
    # Face destruction is rare (Spectral cards, Glass break). Expect ~0.5 procs
    # over a full run, fewer when the ante is high.
    return _gain("j_caino") * 0.5 * (_ante_remaining(ctx.ante) / 8.0)


def _project_hologram_gain(ctx: "SimContext", lt: "LifetimeState") -> float:
    # "Cards added to deck" via Marble/DNA/Spectral: ~2 cards/run when those
    # enablers are owned.
    enablers = {"j_marble", "j_dna"} & ctx.owned_keys
    base_rate = 2.0 if enablers else 0.5
    return _gain("j_hologram") * base_rate * (_ante_remaining(ctx.ante) / 8.0)


def _project_obelisk_gain(ctx: "SimContext", lt: "LifetimeState") -> float:
    # Triggers on a hand that isn't the most-played type. The bot usually has a
    # dominant hand early; assume 0.5 such hands per blind.
    return _gain("j_obelisk") * 0.5 * _blinds_remaining(ctx.ante)


def _project_vampire_gain(ctx: "SimContext", lt: "LifetimeState") -> float:
    # Triggers per enhanced scoring card and CONSUMES the enhancement, so the
    # deck's enhancement stock caps it: density x 5 cards/hand x 3 hands/blind
    # x blinds left, discounted 0.3, at most 10 triggers.
    enhanced_density = sum(ctx.enhancement_density.values()) if ctx.enhancement_density else 0.0
    if enhanced_density <= 0:
        return 0.0
    triggers = enhanced_density * 5.0 * 3.0 * _blinds_remaining(ctx.ante) * 0.3
    return _gain("j_vampire") * min(triggers, 10.0)


def _project_yorick_gain(ctx: "SimContext", lt: "LifetimeState") -> float:
    # One proc per 23 cards discarded. ``yorick_cards_to_proc`` is the distance
    # to the NEXT proc (23 = fresh); future discards are avg per round x rounds left.
    projected_discards = int(lt.avg_discards_per_round * _rounds_remaining(ctx.ante))
    if projected_discards <= 0:
        return 0.0
    procs = 0
    if projected_discards >= lt.yorick_cards_to_proc:
        procs = 1 + (projected_discards - lt.yorick_cards_to_proc) // 23
    return _gain("j_yorick") * procs


def _project_campfire_gain(ctx: "SimContext", lt: "LifetimeState") -> float:
    # Triggers per card sold and RESETS at each boss blind, so the anchor holds
    # only this ante's sells. Project the rest of this ante (~half left on
    # average) plus a soft share of up to two future antes, since the bot may
    # not keep Campfire through the run.
    sells_this_ante = max(0.0, lt.avg_sells_per_ante * 0.5)
    future_antes = _ante_remaining(ctx.ante)
    future_ante_value = min(future_antes, 2) * lt.avg_sells_per_ante * 0.3
    total_sells = sells_this_ante + future_ante_value
    return _gain("j_campfire") * total_sells


def _project_constellation_gain(ctx: "SimContext", lt: "LifetimeState") -> float:
    # Triggers per planet used. Headroom is 10 minus the planets already used,
    # scaled by runway and discounted 0.7.
    projected_additional = max(0, 10 - lt.unique_planets_used)
    remaining_fraction = _ante_remaining(ctx.ante) / 8.0
    return _gain("j_constellation") * projected_additional * remaining_fraction * 0.7


# Hit the Road resets each round, so its value is an average over the round's
# scoring hands rather than a run-long gain: ~1.5 discards x 5 cards x 4/52
# Jacks per round lift the last hands; earlier hands score X1.0.
_HIT_THE_ROAD_AVG_GAIN = 0.15


def _project_hit_the_road_gain(ctx: "SimContext", lt: "LifetimeState") -> float:
    return _HIT_THE_ROAD_AVG_GAIN


def _project_lucky_cat_gain(ctx: "SimContext", lt: "LifetimeState") -> float:
    # Triggers per Lucky card proc (1 in 5): density x 5 x 3 x blinds left x 0.2,
    # at most 8 triggers.
    lucky_density = ctx.enhancement_density.get("LUCKY", 0.0)
    if lucky_density <= 0:
        return 0.0
    triggers = lucky_density * 5.0 * 3.0 * _blinds_remaining(ctx.ante) * 0.2
    return _gain("j_lucky_cat") * min(triggers, 8.0)


def _project_glass_gain(ctx: "SimContext", lt: "LifetimeState") -> float:
    # Triggers per Glass card destroyed (1 in 4 per scored Glass card), at most 4.
    glass_density = ctx.enhancement_density.get("GLASS", 0.0)
    if glass_density <= 0:
        return 0.0
    destroys = glass_density * 5.0 * 3.0 * _blinds_remaining(ctx.ante) * 0.25
    return _gain("j_glass") * min(destroys, 4.0)


_XMULT_PROJECTORS = {
    "j_madness":       _project_madness_gain,
    "j_throwback":     _project_throwback_gain,
    "j_caino":         _project_caino_gain,
    "j_hologram":      _project_hologram_gain,
    "j_obelisk":       _project_obelisk_gain,
    "j_vampire":       _project_vampire_gain,
    "j_yorick":        _project_yorick_gain,
    "j_campfire":      _project_campfire_gain,
    "j_constellation": _project_constellation_gain,
    "j_hit_the_road":  _project_hit_the_road_gain,
    "j_lucky_cat":     _project_lucky_cat_gain,
    "j_glass":         _project_glass_gain,
}

# Jokers whose xmult accrues over time (need runway). Immediate-fire xmult
# (Cavendish, Triboulet, Photograph, Bloodstone) is not in this set; see
# shop_valuation._context_scale for the late-ante carve-out.
SCALING_XMULT_KEYS = frozenset(_XMULT_PROJECTORS.keys())


# Additive scalers (chip/mult): ~3 hands per blind x blinds left, times the
# profile's trigger rate and gain per trigger.
def _hands_remaining(ante: int) -> float:
    return 3.0 * _blinds_remaining(ante)


def _total_xmult(key: str, ctx: "SimContext") -> float:
    if key not in _XMULT_PROJECTORS:
        return 1.0
    lt = ctx.lifetime
    if lt is None:
        return 1.0
    gain = _XMULT_PROJECTORS[key](ctx, lt)
    return lt.xmult_anchor(key) + max(0.0, gain)


def _additive_total(key: str, ctx: "SimContext") -> tuple[float, float]:
    profile = SCALING_REGISTRY.get(key)
    if profile is None or profile.gain_type not in ("chips", "mult", "perma_chips"):
        return (0.0, 0.0)

    live_chips = current_value(ctx.candidate, "chips", 0.0)
    live_mult = current_value(ctx.candidate, "mult", 0.0)

    lt = ctx.lifetime
    gain = 0.0
    if lt is not None and profile.gain_per != 0 and profile.trigger_rate:
        if profile.trigger == "discard":
            events = _rounds_remaining(ctx.ante) * lt.avg_discards_per_round * profile.trigger_rate
        else:
            events = _hands_remaining(ctx.ante) * profile.trigger_rate
        gain = profile.gain_per * events * 0.5  # halve for realization risk

    if profile.gain_type in ("chips", "perma_chips"):
        return (live_chips + gain, live_mult)
    return (live_chips, live_mult + gain)


def project_end_state(ctx: "SimContext") -> dict[str, float | None]:
    """The candidate's projected end-of-run ``xmult``, ``chips`` and ``mult``.

    Each is None when it adds nothing (xmult at most X1.0, chips or mult at
    most 0), so a joker that is not a projected scaler gets all None. The xmult
    anchor is read from the roster (``ctx.lifetime``); the chip and mult
    anchors from the candidate itself.
    """
    key = ctx.candidate_key
    total_xmult = _total_xmult(key, ctx)
    total_chips, total_mult = _additive_total(key, ctx)
    return {
        "xmult": total_xmult if total_xmult > 1.0 else None,
        "chips": total_chips if total_chips > 0 else None,
        "mult": total_mult if total_mult > 0 else None,
    }
