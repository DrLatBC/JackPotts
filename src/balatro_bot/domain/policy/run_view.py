"""The run view: what a joker's value is priced against (see GLOSSARY.md)."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from balatro_bot.domain.models.deck_profile import DeckProfile
from balatro_bot.strategy import compute_strategy

if TYPE_CHECKING:
    from typing import Any

    from balatro_bot.domain.policy.sim_context import LiveRunStats
    from balatro_bot.strategy import Strategy

_ROUND_STATES = frozenset({"SELECTING_HAND", "HAND_PLAYED", "DRAW_TO_HAND"})


@dataclass(frozen=True)
class RunView:
    """Everything about the run a joker's value depends on.

    ``hand_levels`` are the run's own levels, never a boss's halved or reduced
    ones. ``discards`` is what a round starts with. ``boss`` is the current
    blind's name in a round and None elsewhere, where the valuation blends
    the likely bosses.
    """

    roster: tuple[dict, ...]
    hand_levels: dict
    ante: int
    strategy: "Strategy"
    joker_limit: int = 5
    deck_profile: DeckProfile | None = None
    money: int = 0
    discards: int = 0
    live_stats: "LiveRunStats | None" = None
    unique_planets_used: int = 0
    boss: str | None = None

    @classmethod
    def of(
        cls,
        roster: "list[dict] | tuple[dict, ...]",
        hand_levels: dict,
        ante: int,
        *,
        strategy: "Strategy | None" = None,
        joker_limit: int = 5,
        deck_profile: DeckProfile | None = None,
        money: int = 0,
        discards: int = 0,
        live_stats: "LiveRunStats | None" = None,
        unique_planets_used: int = 0,
        boss: str | None = None,
    ) -> "RunView":
        """A run view from its parts; the strategy is built from the roster, levels
        and deck unless given."""
        if strategy is None:
            strategy = compute_strategy(list(roster), hand_levels, deck_profile=deck_profile)
        return cls(
            roster=tuple(roster), hand_levels=hand_levels, ante=ante, strategy=strategy,
            joker_limit=joker_limit, deck_profile=deck_profile, money=money,
            discards=discards, live_stats=live_stats,
            unique_planets_used=unique_planets_used, boss=boss,
        )

    def with_roster(self, roster: "list[dict] | tuple[dict, ...]") -> "RunView":
        """The same run with another roster, and the strategy that roster gives."""
        return replace(
            self, roster=tuple(roster),
            strategy=compute_strategy(list(roster), self.hand_levels, deck_profile=self.deck_profile),
        )


def deck_profile(state: dict[str, Any]) -> DeckProfile:
    """The deck's make-up (draw pile plus hand), cached on the state dict."""
    cached = state.get("_deck_profile")
    if cached is not None:
        return cached
    from balatro_bot.domain.models.card import card_from_dict
    deck_cards = [card_from_dict(c) for c in state.get("cards", {}).get("cards", [])]
    hand_cards = [card_from_dict(c) for c in state.get("hand", {}).get("cards", [])]
    profile = DeckProfile.from_cards(deck_cards + hand_cards)
    state["_deck_profile"] = profile
    return profile


def unique_planets_used(state: dict[str, Any]) -> int:
    """How many different Planet cards the run has used (``consumeable_usage``)."""
    usage = state.get("consumeable_usage", {})
    if not isinstance(usage, dict):
        return 0
    return sum(1 for v in usage.values() if isinstance(v, dict) and v.get("set") == "Planet")


def _current_blind_name(state: dict[str, Any]) -> str | None:
    return next(
        (b.get("name") for b in state.get("blinds", {}).values()
         if isinstance(b, dict) and b.get("status") == "CURRENT"),
        None,
    )


def run_view(state: dict[str, Any]) -> RunView:
    """The run view for this tick, built once and cached on the state dict."""
    cached = state.get("_run_view")
    if cached is not None:
        return cached
    jokers = state.get("jokers", {})
    rnd = state.get("round", {})
    in_round = state.get("state") in _ROUND_STATES
    # The game resets discards_left at cash out, so outside a round it already
    # holds what the next round starts with.
    discards = rnd.get("discards_left", 0)
    if in_round:
        discards += rnd.get("discards_used", 0)
    boss = None
    if in_round and not state.get("_boss_disabled", False):
        boss = _current_blind_name(state)
    view = RunView.of(
        jokers.get("cards", []),
        state.get("hands", {}),
        state.get("ante_num", 1),
        joker_limit=jokers.get("limit", 5),
        deck_profile=deck_profile(state),
        money=state.get("money", 0),
        discards=discards,
        live_stats=state.get("_live_stats"),
        unique_planets_used=unique_planets_used(state),
        boss=boss,
    )
    state["_run_view"] = view
    return view

