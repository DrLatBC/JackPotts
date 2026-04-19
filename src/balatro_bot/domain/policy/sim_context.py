"""Valuation SimContext — bundles every input the scoring sim needs.

Phase 1 of the valuation refactor (see issue #32 / #33). Replaces the ad-hoc
parameter cascade through ``evaluate_joker_value`` → ``_scoring_delta`` →
``_synthetic_hand`` with one frozen dataclass.

Later phases will populate the currently-empty fields (held_cards, lifetime,
round_state, economy, boss) — this scaffold is plumbing-only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from balatro_bot.cards import joker_key
from balatro_bot.joker_effects.parsers import parse_effect_value

if TYPE_CHECKING:
    from balatro_bot.domain.models.deck_profile import DeckProfile
    from balatro_bot.strategy import Strategy


# Maps a scaling-xmult joker key to the ``LifetimeState`` field holding its
# live "Currently X…" anchor. Jokers absent here have no live anchor (either
# because they don't scale xmult, or because their anchor lives in a different
# field like Yorick's remaining-to-proc counter).
_XMULT_ANCHOR_FIELD: dict[str, str] = {
    "j_madness": "madness_xmult",
    "j_hologram": "hologram_xmult",
    "j_canio": "canio_xmult",
    "j_vampire": "vampire_xmult",
    "j_obelisk": "obelisk_xmult",
    "j_yorick": "yorick_xmult",
    "j_campfire": "campfire_xmult",
    "j_constellation": "constellation_xmult",
    "j_throwback": "throwback_xmult",
    "j_hit_the_road": "hit_the_road_xmult",
    "j_lucky_cat": "lucky_cat_xmult",
    "j_glass": "glass_xmult",
}

# Yorick's effect text includes "requires N more" where N ∈ [1, 23]; parse it
# to know how close to the next X1.0 Mult proc we are.
_YORICK_REMAINING_PATTERN = re.compile(r"requires\s+(\d+)\s+more", re.IGNORECASE)


@dataclass(frozen=True)
class LifetimeState:
    """Live run-wide counters for scaling xmult jokers (Phase 4).

    Populated from owned joker effect text via ``from_owned``. For unowned
    candidates the defaults (X1.0 anchors, full /23 proc distance) describe a
    hypothetical joker that hasn't scaled yet.

    Run-rate fields (``avg_discards_per_round``, ``avg_sells_per_ante``) are
    conservative defaults today; issue #41 will thread live bot stats through.
    """

    # Live xmult anchors — "Currently X…" parsed from owned jokers.
    madness_xmult: float = 1.0
    hologram_xmult: float = 1.0
    canio_xmult: float = 1.0
    vampire_xmult: float = 1.0
    obelisk_xmult: float = 1.0
    yorick_xmult: float = 1.0
    campfire_xmult: float = 1.0
    constellation_xmult: float = 1.0
    throwback_xmult: float = 1.0
    hit_the_road_xmult: float = 1.0
    lucky_cat_xmult: float = 1.0
    glass_xmult: float = 1.0

    # Yorick-specific: cards remaining until the next proc. 23 = fresh joker.
    yorick_cards_to_proc: int = 23

    # Run rates (defaults — see issue #41 for live-stats plumbing).
    avg_discards_per_round: float = 1.5
    avg_sells_per_ante: float = 1.5

    # Already threaded through the cascade; lives here to consolidate state.
    unique_planets_used: int = 0

    @classmethod
    def from_owned(
        cls,
        owned_jokers: "list[dict] | tuple[dict, ...]",
        *,
        unique_planets_used: int = 0,
    ) -> "LifetimeState":
        """Build from owned jokers by parsing their effect text."""
        kwargs: dict[str, float | int] = {"unique_planets_used": unique_planets_used}
        for joker in owned_jokers:
            k = joker_key(joker)
            text = joker.get("value", {}).get("effect", "") if isinstance(joker, dict) else ""
            if not text:
                continue
            anchor_field = _XMULT_ANCHOR_FIELD.get(k)
            if anchor_field:
                parsed = parse_effect_value(text)
                xm = parsed.get("xmult")
                if xm is not None and xm > 0:
                    kwargs[anchor_field] = xm
            if k == "j_yorick":
                m = _YORICK_REMAINING_PATTERN.search(text)
                if m:
                    kwargs["yorick_cards_to_proc"] = max(1, int(m.group(1)))
        return cls(**kwargs)


@dataclass(frozen=True)
class BossBlindState:
    """Phase 7: boss-blind effects threaded into the shop sim.

    Construct via ``from_name`` (templates for the ~8 impactful bosses) or
    directly for tests. ``name`` is the display name (e.g. "The Plant"),
    matching ``RoundContext.blind_name``. All flag fields default to off so
    an empty instance is a no-op.
    """

    name: str = ""
    # Card-level debuffs applied while the boss is active
    debuffs_faces: bool = False           # The Plant
    debuffs_suit: str | None = None       # The Club / Window / Head / Goad etc.
    # Hand/discard/size deltas vs baseline
    hand_size_delta: int = 0              # The Manacle: -1
    hands_delta: int = 0                  # The Needle: -N (down to 1)
    discards_delta: int = 0               # The Hook: random discards forced (modelled as -2 effective)
    # Hand-level deltas
    hand_levels_halved: bool = False      # The Flint
    hand_levels_reduced: bool = False     # The Arm
    # Scoring-suit restriction (only matching suit scores)
    scoring_suit: str | None = None
    # Eye: can't replay a hand type this round
    excludes_repeat_hand: bool = False
    # Pillar: previously scored cards can't score again this ante
    pillar_replay_lock: bool = False
    # Mouth: hand type locked to first played
    locks_hand_type: bool = False

    @classmethod
    def from_name(cls, name: str) -> "BossBlindState | None":
        tmpl = _BOSS_TEMPLATES.get(name)
        if tmpl is None:
            return None
        return cls(name=name, **tmpl)


_BOSS_TEMPLATES: dict[str, dict] = {
    "The Plant":   {"debuffs_faces": True},
    "The Needle":  {"hands_delta": -3},  # 4 base → 1
    "The Hook":    {"discards_delta": -2},
    "The Manacle": {"hand_size_delta": -1},
    "The Pillar":  {"pillar_replay_lock": True},
    "The Arm":     {"hand_levels_reduced": True},
    "The Flint":   {"hand_levels_halved": True},
    "The Eye":     {"excludes_repeat_hand": True},
    "The Mouth":   {"locks_hand_type": True},
    "The Head":    {"scoring_suit": "H"},
    "The Club":    {"scoring_suit": "C"},
    "The Window":  {"scoring_suit": "D"},
    "The Goad":    {"debuffs_suit": "S"},
}

# Weights for shop-phase "average boss ahead" blend. Covers the top-impact
# bosses; sums to 1.0 across entries we actually template.
BOSS_WEIGHT: dict[str, float] = {
    "The Plant":   0.14,
    "The Needle":  0.12,
    "The Hook":    0.14,
    "The Manacle": 0.10,
    "The Pillar":  0.08,
    "The Arm":     0.08,
    "The Eye":     0.08,
    "The Mouth":   0.06,
    "The Head":    0.05,
    "The Club":    0.05,
    "The Window":  0.05,
    "The Goad":    0.05,
}


@dataclass(frozen=True)
class SimContext:
    candidate: dict
    owned_jokers: tuple[dict, ...]
    hand_levels: dict[str, dict]
    strategy: "Strategy"
    ante: int
    joker_limit: int = 5
    deck_profile: "DeckProfile | None" = None
    unique_planets_used: int = 0

    # Populated incrementally by later phases:
    held_cards: tuple[dict, ...] = ()
    lifetime: "LifetimeState | None" = None
    round_state: object | None = None
    economy: object | None = None
    boss: BossBlindState | None = None

    # Phase 8: Monte Carlo sample count. When > 0, the scoring sim runs N
    # trials with RNG-driven branch selections (Misprint roll, Lucky card
    # trigger, Bloodstone trigger) and returns the mean. Default 0 keeps the
    # fast expected-value path for non-stochastic candidates.
    monte_carlo_samples: int = 0

    # Derived
    candidate_key: str = field(default="", repr=False)
    owned_keys: frozenset[str] = field(default_factory=frozenset, repr=False)

    # Phase 3: density (fractions summing to ~1.0 per dimension) from deck_profile.
    # Empty dict when deck_profile is None; callers must treat that as "no signal".
    rank_density: dict[str, float] = field(default_factory=dict, repr=False)
    suit_density: dict[str, float] = field(default_factory=dict, repr=False)
    enhancement_density: dict[str, float] = field(default_factory=dict, repr=False)

    @classmethod
    def build(
        cls,
        *,
        candidate: dict,
        owned_jokers: list[dict],
        hand_levels: dict[str, dict],
        strategy: "Strategy",
        ante: int,
        joker_limit: int = 5,
        deck_profile: "DeckProfile | None" = None,
        unique_planets_used: int = 0,
        blind_name: str | None = None,
        monte_carlo_samples: int = 0,
    ) -> "SimContext":
        rank_density: dict[str, float] = {}
        suit_density: dict[str, float] = {}
        enhancement_density: dict[str, float] = {}
        if deck_profile is not None and deck_profile.total_cards > 0:
            total = deck_profile.total_cards
            rank_density = {r: c / total for r, c in deck_profile.rank_counts.items()}
            suit_density = {s: c / total for s, c in deck_profile.suit_counts.items()}
            enhancement_density = {
                e: c / total for e, c in deck_profile.enhancement_counts.items()
            }
        lifetime = LifetimeState.from_owned(
            owned_jokers, unique_planets_used=unique_planets_used,
        )
        boss = BossBlindState.from_name(blind_name) if blind_name else None
        return cls(
            candidate=candidate,
            owned_jokers=tuple(owned_jokers),
            hand_levels=hand_levels,
            strategy=strategy,
            ante=ante,
            joker_limit=joker_limit,
            deck_profile=deck_profile,
            unique_planets_used=unique_planets_used,
            candidate_key=candidate.get("key", "") or joker_key(candidate),
            owned_keys=frozenset(joker_key(j) for j in owned_jokers),
            rank_density=rank_density,
            suit_density=suit_density,
            enhancement_density=enhancement_density,
            lifetime=lifetime,
            boss=boss,
            monte_carlo_samples=monte_carlo_samples,
        )
