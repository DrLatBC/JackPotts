"""RoundContext — pre-computed facts about the current round for rules to use."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from balatro_bot.domain.scoring.base import arm_reduce_hand_levels, flint_halve_hand_levels
from balatro_bot.domain.scoring.search import HandCandidate, best_hand
from balatro_bot.infrastructure.state_adapter import adapt_state
from balatro_bot.strategy import Strategy, compute_strategy

if TYPE_CHECKING:
    from typing import Any

    from balatro_bot.domain.models.snapshot import Snapshot

# ---------------------------------------------------------------------------
# Round outlook thresholds (projected score / chips remaining)
# ---------------------------------------------------------------------------
_COMFORTABLE_RATIO = 1.5   # >= this → comfortable, blind is well in hand
_TIGHT_RATIO = 0.8         # >= this → tight, every hand matters

__all__ = ["RoundContext"]


@dataclass
class RoundContext:
    blind_score: int
    blind_name: str
    chips_scored: int
    chips_remaining: int
    hands_left: int
    discards_left: int
    hand_cards: list[dict]
    hand_levels: dict[str, dict]
    jokers: list[dict]
    best: HandCandidate | None
    money: int
    ante: int
    round_num: int
    min_cards: int
    strategy: Strategy
    deck_cards: list[dict]
    mouth_locked_hand: str | None = None
    score_discount: float = 1.0
    forced_card_idx: int | None = None
    ancient_suit: str | None = None
    eye_used_hands: set[str] | None = None
    scoring_suit: str | None = None

    @property
    def round_outlook(self) -> str:
        """Project whether the round is winnable at the current scoring rate.

        Returns one of:
          "won"         — chips_remaining <= 0, blind already beaten
          "comfortable" — projected output covers blind with margin (>=1.5x)
          "tight"       — projected is close, every hand matters (0.8x–1.5x)
          "hopeless"    — projected can't reach the blind (<0.8x)
        """
        if self.chips_remaining <= 0:
            return "won"
        effective = self.best.total * self.score_discount if self.best else 0
        if effective <= 0:
            return "hopeless"
        projected = effective * self.hands_left
        ratio = projected / self.chips_remaining
        if ratio >= _COMFORTABLE_RATIO:
            return "comfortable"
        elif ratio >= _TIGHT_RATIO:
            return "tight"
        else:
            return "hopeless"

    @staticmethod
    def from_state(state: dict[str, Any]) -> RoundContext:
        """Build RoundContext from a raw API state dict.

        Calls adapt_state() + from_snapshot() internally.
        Caches the result on the state dict for per-tick reuse.
        """
        cached = state.get("_round_ctx")
        if cached is not None:
            return cached

        snapshot = adapt_state(state)
        ctx = RoundContext.from_snapshot(snapshot)
        state["_round_ctx"] = ctx
        return ctx

    @staticmethod
    def from_snapshot(snapshot: Snapshot) -> RoundContext:
        """Build RoundContext from a typed Snapshot.

        All domain logic lives here. No raw dict access.
        """
        hand_cards = snapshot.hand_cards
        hand_levels = snapshot.hand_levels
        jokers = snapshot.jokers
        money = snapshot.money
        deck_cards = snapshot.deck_cards
        blind_name = snapshot.current_blind.name
        blind_score = snapshot.current_blind.score

        boss_disabled = snapshot.current_blind.boss_disabled

        # Boss blind mutations — skip when Luchador has disabled the boss
        if not boss_disabled:
            if blind_name == "The Flint":
                hand_levels = flint_halve_hand_levels(hand_levels)
            if blind_name == "The Arm":
                hand_levels = arm_reduce_hand_levels(hand_levels)

        min_cards = 5 if blind_name == "The Psychic" and not boss_disabled else 1

        mouth_locked_hand = None
        if blind_name == "The Mouth" and not boss_disabled:
            for hand_name, hand_data in hand_levels.items():
                if isinstance(hand_data, dict) and hand_data.get("played_this_round", 0) > 0:
                    mouth_locked_hand = hand_name
                    break

        eye_used_hands = None
        if blind_name == "The Eye" and not boss_disabled:
            eye_used_hands = {
                ht for ht, data in hand_levels.items()
                if isinstance(data, dict) and data.get("played_this_round", 0) > 0
            }

        scoring_suit = None
        if not boss_disabled:
            if blind_name == "The Head":
                scoring_suit = "H"
            elif blind_name == "The Club":
                scoring_suit = "C"
            elif blind_name == "The Window":
                scoring_suit = "D"

        chips_scored = snapshot.round.chips
        hands_left = snapshot.round.hands_left
        discards_left = snapshot.round.discards_left

        joker_count = len(jokers)
        score_discount = (
            (joker_count - 1) / joker_count if blind_name == "Crimson Heart" and joker_count > 1 and not boss_disabled
            else 0.5 if blind_name == "Crimson Heart" and not boss_disabled
            else 1.0
        )

        forced_card_idx = None
        if blind_name == "Cerulean Bell" and not boss_disabled:
            for i, c in enumerate(hand_cards):
                s = c.get("state", {})
                if isinstance(s, dict) and s.get("highlight"):
                    forced_card_idx = i
                    break

        ancient_suit = snapshot.round.ancient_suit
        ox_most_played = snapshot.round.most_played_poker_hand if blind_name == "The Ox" and not boss_disabled else None
        strat = compute_strategy(jokers, hand_levels)
        # When boss is disabled (Luchador sold), clear blind_name for scoring
        # so boss-specific scoring effects (The Tooth, The Ox, etc.) don't fire.
        effective_blind = "" if boss_disabled else blind_name

        return RoundContext(
            blind_score=blind_score,
            blind_name=effective_blind,
            chips_scored=chips_scored,
            chips_remaining=blind_score - chips_scored,
            hands_left=hands_left,
            discards_left=discards_left,
            hand_cards=hand_cards,
            hand_levels=hand_levels,
            jokers=jokers,
            best=best_hand(
                hand_cards, hand_levels,
                min_select=min_cards, jokers=jokers,
                money=money, discards_left=discards_left,
                hands_left=hands_left,
                joker_limit=snapshot.joker_limit,
                required_hand=mouth_locked_hand,
                required_card_indices={forced_card_idx} if forced_card_idx is not None else None,
                ancient_suit=ancient_suit,
                excluded_hands=eye_used_hands,
                deck_cards=deck_cards,
                blind_name=effective_blind,
                ox_most_played=ox_most_played,
            ),
            mouth_locked_hand=mouth_locked_hand,
            score_discount=score_discount,
            forced_card_idx=forced_card_idx,
            ancient_suit=ancient_suit,
            eye_used_hands=eye_used_hands,
            scoring_suit=scoring_suit,
            money=money,
            ante=snapshot.ante,
            round_num=snapshot.round_num,
            min_cards=min_cards,
            strategy=strat,
            deck_cards=deck_cards,
        )
