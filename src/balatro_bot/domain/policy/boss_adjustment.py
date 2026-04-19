"""Phase 7: boss-blind-aware multiplier on ``evaluate_joker_value``.

The scoring sim doesn't simulate boss debuffs. Rather than rewriting
``_synthetic_hand`` to thread every boss effect, this module applies a
post-sim multiplier based on which boss is active (if mid-round) or a
weighted average over common bosses (shop phase).

Typical return values: 1.0 = neutral, <1.0 = joker weaker under this boss,
>1.0 = joker stronger under this boss.
"""

from __future__ import annotations

from balatro_bot.domain.policy.sim_context import BOSS_WEIGHT, BossBlindState

# ---------------------------------------------------------------------------
# Per-boss sensitivity groups
# ---------------------------------------------------------------------------

# Face-card-dependent scorers (Plant debuffs faces)
_FACE_DEPENDENT: frozenset[str] = frozenset({
    "j_photograph", "j_triboulet", "j_smiley", "j_scary_face",
    "j_sock_and_buskin", "j_pareidolia", "j_reserved_parking",
    "j_business", "j_faceless", "j_midas_mask",
})

# Single-hand burst jokers (Needle: only 1 hand). Worth more when every hand
# is the final hand.
_SINGLE_HAND_BURST: frozenset[str] = frozenset({
    "j_acrobat", "j_card_sharp", "j_mr_bones", "j_chicot",
    "j_dusk",  # final-hand retrigger
})

# Per-hand-played scalers (need many hands to compound) — Needle hurts them.
_PER_HAND_SCALERS: frozenset[str] = frozenset({
    "j_green_joker", "j_ride_the_bus", "j_obelisk", "j_supernova",
    "j_space", "j_constellation",
})

# Discard-scalers (Hook forces extra discards → more triggers)
_DISCARD_SCALERS: frozenset[str] = frozenset({
    "j_yorick", "j_castle", "j_green_joker", "j_hit_the_road",
    "j_mail", "j_trading", "j_faceless",
})

# Held-in-hand jokers (Manacle: -1 hand size cuts held triggers)
_HELD_PHASE: frozenset[str] = frozenset({
    "j_baron", "j_shoot_the_moon", "j_raised_fist", "j_mime",
    "j_blackboard", "j_reserved_parking",
})

# Per-scoring-card enhancement jokers (Pillar: scored cards can't rescore).
# Worst-hit are jokers whose best targets are specific scoring cards.
_ENHANCEMENT_PER_CARD: frozenset[str] = frozenset({
    "j_steel_joker", "j_glass", "j_lucky_cat",
})

# Suit-specific scorers — boosted when their suit is the scoring_suit,
# devalued when debuffed_suit matches.
_SUIT_OF: dict[str, str] = {
    "j_greedy_joker":    "D",
    "j_lusty_joker":     "H",
    "j_wrathful_joker":  "S",
    "j_gluttenous_joker":"C",
    "j_bloodstone":      "H",
    "j_onyx_agate":      "C",
    "j_arrowhead":       "S",
    "j_rough_gem":       "D",  # economy suit-specific
}

# Hand-type-locked jokers — Eye (no repeat hand), Mouth (locks first hand).
_HAND_TYPE_LOCKED: frozenset[str] = frozenset({
    "j_duo", "j_trio", "j_family", "j_order", "j_tribe", "j_seeing_double",
})

# Planet-reliant jokers (Arm reduces hand levels → their scaled base drops)
_PLANET_RELIANT: frozenset[str] = frozenset({
    "j_space", "j_burnt", "j_satellite", "j_astronomer",
})


# ---------------------------------------------------------------------------
# Per-boss adjustment
# ---------------------------------------------------------------------------

def _adjust_for_boss(key: str, boss: BossBlindState) -> float:
    """Multiplier applied to ``base_value`` for a given (joker, boss) pair.

    1.0 means no effect. Multipliers compose, but we only apply a single
    boss at a time so the combination layer is trivial.
    """
    mult = 1.0

    # Plant — face debuff zeros face-card jokers
    if boss.debuffs_faces and key in _FACE_DEPENDENT:
        return 0.05  # collapse to near-zero; allow a sliver so ordering is stable

    # Needle — single hand this round
    if boss.hands_delta <= -2:
        if key in _SINGLE_HAND_BURST:
            mult *= 1.4
        elif key in _PER_HAND_SCALERS:
            mult *= 0.6

    # Hook — two forced random discards
    if boss.discards_delta <= -1:
        if key in _DISCARD_SCALERS:
            mult *= 1.3

    # Manacle — -1 hand size cuts held triggers
    if boss.hand_size_delta <= -1 and key in _HELD_PHASE:
        mult *= 0.75

    # Pillar — previously scored cards can't rescore
    if boss.pillar_replay_lock and key in _ENHANCEMENT_PER_CARD:
        mult *= 0.8

    # Arm — reduced hand levels
    if boss.hand_levels_reduced and key in _PLANET_RELIANT:
        mult *= 0.8

    # Eye / Mouth — constrains hand-type options
    if (boss.excludes_repeat_hand or boss.locks_hand_type) and key in _HAND_TYPE_LOCKED:
        mult *= 0.75

    # Scoring-suit restriction (Head/Club/Window)
    if boss.scoring_suit and key in _SUIT_OF:
        if _SUIT_OF[key] == boss.scoring_suit:
            mult *= 1.2
        else:
            mult *= 0.5

    # Suit debuff (Goad) — suit-specific scorers lose their target suit
    if boss.debuffs_suit and key in _SUIT_OF:
        if _SUIT_OF[key] == boss.debuffs_suit:
            mult *= 0.3

    return mult


def boss_multiplier(key: str, boss: BossBlindState | None) -> float:
    """In-round adjustment for a known active boss.

    Returns 1.0 when ``boss`` is None (no active boss / boss disabled by
    Luchador) or when the joker has no sensitivity.
    """
    if boss is None or not boss.name:
        return 1.0
    return _adjust_for_boss(key, boss)


def shop_blended_multiplier(key: str) -> float:
    """Shop-phase: weighted average over upcoming boss pool.

    Used when the caller has no active boss (shop, between rounds). Blends
    the joker's per-boss adjustment by ``BOSS_WEIGHT``. Faces-dependent
    jokers eat a ~15% systemic penalty (Plant hits them hard and ~14% of
    bosses are Plant); single-hand-burst jokers get a small uplift.
    """
    total = 0.0
    for boss_name, weight in BOSS_WEIGHT.items():
        boss = BossBlindState.from_name(boss_name)
        if boss is None:
            continue
        total += weight * _adjust_for_boss(key, boss)
    return total + (1.0 - sum(BOSS_WEIGHT.values()))
