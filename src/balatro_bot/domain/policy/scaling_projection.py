"""Per-joker projection of scaling-joker end-of-run state.

Phase 4 of the valuation refactor (see issue #36). Replaces the two bolted-on
floors in ``evaluate_joker_value`` (the flat ``milk_priority`` runway and the
``projected_xmult_gain`` block) with a live-anchor-aware projection per joker.

For each scaling joker, ``project_end_state`` returns a ``(chips, mult, xmult)``
tuple representing the *total* value the joker is expected to reach by end of
run, combining its live anchor (parsed from effect text when owned) with a
forward projection based on its trigger rate × rounds remaining. The caller
converts the tuple to a floor via the same ``_dynamic_power`` conversion used
for the live-anchor-only floor.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from balatro_bot.joker_effects.parsers import parse_effect_value
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


# Per-joker projection functions. Each returns the *gain over baseline X1.0*
# for xmult scalers, or the additive gain for mult/chips scalers. The caller
# adds the live anchor to this gain to get end-of-run state.

def _project_madness_gain(ctx: "SimContext", lt: "LifetimeState") -> float:
    # +X0.5 per blind select, but Madness destroys a random joker each blind —
    # halve the naive projection to account for joker-destroy risk muting the
    # realized value (destroyed jokers still triggered Madness gain, but their
    # own contribution is lost, lowering overall roster power).
    return 0.5 * _blinds_remaining(ctx.ante) * 0.5


def _project_throwback_gain(ctx: "SimContext", lt: "LifetimeState") -> float:
    # Bot skip policy averages ~2 blind skips per run. Scale by remaining runway.
    remaining_fraction = _ante_remaining(ctx.ante) / 8.0
    return 2.0 * 0.25 * remaining_fraction


def _project_canio_gain(ctx: "SimContext", lt: "LifetimeState") -> float:
    # Face destruction is rare (Spectral cards, Glass break). Expect ~0.5 procs
    # over a full run — less when ante is high.
    return 0.5 * (_ante_remaining(ctx.ante) / 8.0)


def _project_hologram_gain(ctx: "SimContext", lt: "LifetimeState") -> float:
    # "Cards added to deck" via Marble/DNA/Spectral. ~2 cards/run avg when
    # those enablers exist; gate by roster presence for accuracy.
    owned = ctx.owned_keys
    enablers = {"j_marble", "j_dna"} & owned
    base_rate = 2.0 if enablers else 0.5
    return 0.25 * base_rate * (_ante_remaining(ctx.ante) / 8.0)


def _project_obelisk_gain(ctx: "SimContext", lt: "LifetimeState") -> float:
    # +X0.2 per hand that isn't the most-played type. Bot typically has a
    # dominant hand early; assume 0.5 non-fav hands per blind.
    return 0.2 * 0.5 * _blinds_remaining(ctx.ante)


def _project_vampire_gain(ctx: "SimContext", lt: "LifetimeState") -> float:
    # +X0.1 per enhanced scoring card; Vampire CONSUMES the enhancement, so
    # gain is capped by deck's enhancement stock. Use density to estimate.
    enhanced_density = sum(ctx.enhancement_density.values()) if ctx.enhancement_density else 0.0
    # Roughly: enhanced_density × 5 cards/hand × 3 hands/blind × blinds_left,
    # heavily discounted (0.3) because Vampire eats its own food supply.
    if enhanced_density <= 0:
        return 0.0
    triggers = enhanced_density * 5.0 * 3.0 * _blinds_remaining(ctx.ante) * 0.3
    return 0.1 * min(triggers, 10.0)  # cap at +1.0 xmult projection


def _project_yorick_gain(ctx: "SimContext", lt: "LifetimeState") -> float:
    # +X1.0 per 23 cards discarded lifetime. ``yorick_cards_to_proc`` is the
    # distance to the NEXT proc (23 = fresh). Project future discards from
    # avg_discards_per_round × rounds_left.
    projected_discards = int(lt.avg_discards_per_round * _rounds_remaining(ctx.ante))
    if projected_discards <= 0:
        return 0.0
    procs = 0
    if projected_discards >= lt.yorick_cards_to_proc:
        procs = 1 + (projected_discards - lt.yorick_cards_to_proc) // 23
    return 1.0 * procs


def _project_campfire_gain(ctx: "SimContext", lt: "LifetimeState") -> float:
    # +X0.25 per sold card, RESETS at each boss blind. Live anchor captures
    # accumulation within the current ante. Future antes contribute one-ante
    # worth each, but each ante resets, so the floor is one-ante-at-a-time.
    # Project: remaining sells this ante + one-ante worth × a few future antes.
    sells_this_ante = max(0.0, lt.avg_sells_per_ante * 0.5)  # ~half-ante left on avg
    future_antes = _ante_remaining(ctx.ante)
    # Future antes reset — value only ~1 ante's worth since we pay now but
    # gain is soft (bot may not own Campfire through the full run).
    future_ante_value = min(future_antes, 2) * lt.avg_sells_per_ante * 0.3
    total_sells = sells_this_ante + future_ante_value
    return 0.25 * total_sells


def _project_constellation_gain(ctx: "SimContext", lt: "LifetimeState") -> float:
    # +X0.1 per planet used lifetime. ``unique_planets_used`` counts done;
    # project remaining planet usage. Arcana/Celestial pack income gives
    # ~5 planets/run typical; cap headroom at 10.
    projected_additional = max(0, 10 - lt.unique_planets_used)
    remaining_fraction = _ante_remaining(ctx.ante) / 8.0
    return 0.1 * projected_additional * remaining_fraction * 0.7


def _project_hit_the_road_gain(ctx: "SimContext", lt: "LifetimeState") -> float:
    # +X0.5 per Jack discarded, RESETS each round. Four Jacks in 52 = 0.077.
    # ~1.5 discards × 5 cards × 0.077 ≈ 0.58 Jacks/round. Use 0.25 xmult avg.
    return 0.25 * _rounds_remaining(ctx.ante)


def _project_lucky_cat_gain(ctx: "SimContext", lt: "LifetimeState") -> float:
    # +X0.25 per Lucky card trigger. Density-driven: 4 Lucky in deck is
    # ~baseline value, 8+ is strong.
    lucky_density = ctx.enhancement_density.get("LUCKY", 0.0)
    if lucky_density <= 0:
        return 0.0
    triggers = lucky_density * 5.0 * 3.0 * _blinds_remaining(ctx.ante) * 0.2
    # Lucky proc rate is 1/5, so multiply triggers by 0.2 (already factored).
    return 0.25 * min(triggers, 8.0)


def _project_glass_gain(ctx: "SimContext", lt: "LifetimeState") -> float:
    # Glass Joker gains +X0.75 per Glass card destroyed. gain_per in the
    # registry is 0 (variable), so model directly from deck density.
    glass_density = ctx.enhancement_density.get("GLASS", 0.0)
    if glass_density <= 0:
        return 0.0
    # ~1/4 destruction chance × triggers × blinds_left.
    destroys = glass_density * 5.0 * 3.0 * _blinds_remaining(ctx.ante) * 0.25
    return 0.75 * min(destroys, 4.0)


_XMULT_PROJECTORS = {
    "j_madness":       _project_madness_gain,
    "j_throwback":     _project_throwback_gain,
    "j_canio":         _project_canio_gain,
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


_XMULT_ANCHOR_ATTR: dict[str, str] = {
    "j_madness":       "madness_xmult",
    "j_throwback":     "throwback_xmult",
    "j_canio":         "canio_xmult",
    "j_hologram":      "hologram_xmult",
    "j_obelisk":       "obelisk_xmult",
    "j_vampire":       "vampire_xmult",
    "j_yorick":        "yorick_xmult",
    "j_campfire":      "campfire_xmult",
    "j_constellation": "constellation_xmult",
    "j_hit_the_road":  "hit_the_road_xmult",
    "j_lucky_cat":     "lucky_cat_xmult",
    "j_glass":         "glass_xmult",
}


def project_total_xmult(key: str, ctx: "SimContext") -> float:
    """Return projected end-of-run xmult for a scaling xmult joker.

    Combines live anchor (``LifetimeState.{joker}_xmult``, X1.0 if unowned)
    with projected future gain. Returns X1.0 for jokers we don't project (the
    caller treats X1.0 as "no floor contribution").
    """
    if key not in _XMULT_PROJECTORS:
        return 1.0
    lt = ctx.lifetime
    if lt is None:
        return 1.0
    anchor_attr = _XMULT_ANCHOR_ATTR.get(key)
    live_anchor = getattr(lt, anchor_attr, 1.0) if anchor_attr else 1.0
    projector = _XMULT_PROJECTORS[key]
    gain = projector(ctx, lt)
    return live_anchor + max(0.0, gain)


# Additive-gain scalers (chip/mult). These used to get the milk_priority
# runway floor. Modelled here as: bot plays ~3 hands/blind × blinds_left,
# multiplied by trigger probability and gain_per. For fresh candidates this
# replaces the flat 2/3/4 runway with something trigger-rate-aware.
def _hands_remaining(ante: int) -> float:
    return 3.0 * _blinds_remaining(ante)


# Per-hand trigger probability for each mult/chip scaler. Values are rough
# but trigger-aware (e.g. Runner only fires on Straights which the bot builds
# toward rarely unless a straight archetype is active).
_ADDITIVE_PROJECTORS: dict[str, callable] = {
    "j_green_joker":    lambda ctx, lt: 1.0,       # fires every hand
    "j_supernova":      lambda ctx, lt: 1.0,       # fires every hand (stale-type)
    "j_ride_the_bus":   lambda ctx, lt: 0.6,       # reset risk on face hands
    "j_square":         lambda ctx, lt: 0.3,       # requires exactly 4 cards
    "j_runner":         lambda ctx, lt: 0.15,      # only straights
    "j_trousers":       lambda ctx, lt: 0.25,      # only two pair
    "j_hiker":          lambda ctx, lt: 5.0,       # +5 per card scored, ~5/hand → 25/hand
    "j_wee":            lambda ctx, lt: 0.2,       # 2s in hand
    "j_castle":         lambda ctx, lt: 0.5,       # per discarded suit card
}


def project_additive_total(key: str, ctx: "SimContext", effect_text: str) -> tuple[float, float]:
    """Return (chips_total, mult_total) = live anchor + projected future gain.

    Live anchor for additive scalers comes from ``effect_text`` (parsed
    "Currently +N Chips/Mult"); projection adds expected future gain from
    rounds remaining × trigger rate.

    Only produces non-zero output for scalers with gain_type ∈
    {"chips", "mult", "perma_chips"}. xmult scalers use ``project_total_xmult``.
    """
    profile = SCALING_REGISTRY.get(key)
    if profile is None or profile.gain_type not in ("chips", "mult", "perma_chips"):
        return (0.0, 0.0)

    # Live anchor from effect text (may be the per-trigger increment for
    # never-scaled jokers — parse_effect_value already prefers "Currently").
    parsed = parse_effect_value(effect_text) if effect_text else {}
    live_chips = parsed.get("chips") or 0.0
    live_mult = parsed.get("mult") or 0.0

    # Projected future gain.
    lt = ctx.lifetime
    gain = 0.0
    if lt is not None and profile.gain_per != 0 and key in _ADDITIVE_PROJECTORS:
        per_hand_trigger = _ADDITIVE_PROJECTORS[key](ctx, lt)
        if profile.trigger == "discard":
            events = _rounds_remaining(ctx.ante) * lt.avg_discards_per_round * per_hand_trigger
        else:
            events = _hands_remaining(ctx.ante) * per_hand_trigger
        gain = profile.gain_per * events * 0.5  # halve for realization risk

    if profile.gain_type in ("chips", "perma_chips"):
        return (live_chips + gain, live_mult)
    # mult
    return (live_chips, live_mult + gain)
