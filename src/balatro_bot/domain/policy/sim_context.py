"""Valuation SimContext — bundles every input the scoring sim needs.

One frozen dataclass threaded through ``joker_value`` →
``_scoring_delta`` → ``_synthetic_hand``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from balatro_bot.cards import joker_key
from balatro_bot.joker_effects.parsers import current_counter, current_value
from balatro_bot.scaling import SCALING_REGISTRY

if TYPE_CHECKING:
    from balatro_bot.domain.models.deck_profile import DeckProfile
    from balatro_bot.domain.policy.run_view import RunView
    from balatro_bot.strategy import Strategy


@dataclass(frozen=True)
class LiveRunStats:
    """Bot-observed per-run averages, threaded into ``LifetimeState``.

    Populated by ``bot.py`` from ``GameLoopState`` each tick and attached to
    ``state['_live_stats']``. When absent, ``LifetimeState`` falls back to its
    own conservative defaults.
    """

    avg_discards_per_round: float = 1.5
    avg_sells_per_ante: float = 1.5
    avg_plays_per_round: float = 2.5


@dataclass(frozen=True)
class LifetimeState:
    """Live run-wide counters for scaling jokers.

    Built from the roster's current values (``joker_effects/live_values``) via
    ``from_owned``. A joker not in the roster reads X1.0 and a full /23 proc
    distance: a hypothetical joker that hasn't scaled yet.

    Run-rate fields (``avg_discards_per_round``, ``avg_sells_per_ante``) are
    conservative defaults; ``from_owned`` overrides them from ``LiveRunStats``
    when the bot passes its observed averages.
    """

    # Owned xmult scalers' current xmult, by joker key.
    xmult_anchors: dict[str, float] = field(default_factory=dict)

    # Yorick-specific: cards remaining until the next proc. 23 = fresh joker.
    yorick_cards_to_proc: int = 23

    # Run rates; overridden from ``LiveRunStats`` in ``from_owned``.
    avg_discards_per_round: float = 1.5
    avg_sells_per_ante: float = 1.5

    unique_planets_used: int = 0

    @classmethod
    def from_owned(
        cls,
        owned_jokers: "list[dict] | tuple[dict, ...]",
        *,
        unique_planets_used: int = 0,
        live_stats: "LiveRunStats | None" = None,
    ) -> "LifetimeState":
        """Build from owned jokers' current values (live_values).

        When ``live_stats`` is provided, override the conservative defaults for
        ``avg_discards_per_round`` / ``avg_sells_per_ante`` with the bot's
        observed per-run averages.
        """
        kwargs: dict = {"unique_planets_used": unique_planets_used}
        anchors: dict[str, float] = {}
        if live_stats is not None:
            kwargs["avg_discards_per_round"] = live_stats.avg_discards_per_round
            kwargs["avg_sells_per_ante"] = live_stats.avg_sells_per_ante
        for joker in owned_jokers:
            k = joker_key(joker)
            profile = SCALING_REGISTRY.get(k)
            if profile is not None and profile.gain_type == "xmult":
                xm = current_value(joker, "xmult", 0.0)
                if xm > 0:
                    anchors[k] = xm
            if k == "j_yorick":
                remaining = current_counter(joker)
                if remaining is not None:
                    kwargs["yorick_cards_to_proc"] = max(1, remaining)
        return cls(xmult_anchors=anchors, **kwargs)

    def xmult_anchor(self, key: str) -> float:
        """The owned joker's current xmult; X1.0 when it isn't owned."""
        return self.xmult_anchors.get(key, 1.0)


@dataclass(frozen=True)
class BossBlindState:
    """Boss-blind effects threaded into the shop sim.

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

    # ``build`` fills ``lifetime`` and ``boss``; the others default empty.
    held_cards: tuple[dict, ...] = ()
    lifetime: "LifetimeState | None" = None
    round_state: object | None = None
    economy: object | None = None
    boss: BossBlindState | None = None

    # Monte Carlo sample count. When > 0, the scoring sim runs N
    # trials with RNG-driven branch selections (Misprint roll, Lucky card
    # trigger, Bloodstone trigger) and returns the mean. Default 0 keeps the
    # fast expected-value path for non-stochastic candidates.
    monte_carlo_samples: int = 0

    # Wallet and the discards a round starts with, for money-scaling jokers
    # (Bull, Bootstraps) and discard-scaling jokers (Banner, Mystic Summit).
    money: int = 0
    discards_left: int = 0

    # Derived
    candidate_key: str = field(default="", repr=False)
    owned_keys: frozenset[str] = field(default_factory=frozenset, repr=False)

    # Density (fractions summing to ~1.0 per dimension) from deck_profile.
    # Empty dict when deck_profile is None; callers must treat that as "no signal".
    rank_density: dict[str, float] = field(default_factory=dict, repr=False)
    suit_density: dict[str, float] = field(default_factory=dict, repr=False)
    enhancement_density: dict[str, float] = field(default_factory=dict, repr=False)

    @classmethod
    def build(
        cls,
        candidate: dict,
        run: "RunView",
        *,
        monte_carlo_samples: int = 0,
    ) -> "SimContext":
        deck_profile = run.deck_profile
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
            run.roster,
            unique_planets_used=run.unique_planets_used,
            live_stats=run.live_stats,
        )
        boss = BossBlindState.from_name(run.boss) if run.boss else None
        return cls(
            candidate=candidate,
            owned_jokers=run.roster,
            hand_levels=run.hand_levels,
            strategy=run.strategy,
            ante=run.ante,
            joker_limit=run.joker_limit,
            deck_profile=deck_profile,
            unique_planets_used=run.unique_planets_used,
            candidate_key=candidate.get("key", "") or joker_key(candidate),
            owned_keys=frozenset(joker_key(j) for j in run.roster),
            rank_density=rank_density,
            suit_density=suit_density,
            enhancement_density=enhancement_density,
            lifetime=lifetime,
            boss=boss,
            monte_carlo_samples=monte_carlo_samples,
            money=run.money,
            discards_left=run.discards,
        )
