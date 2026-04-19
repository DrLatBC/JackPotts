"""RoundContext — pre-computed facts about the current round for rules to use."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from balatro_bot.domain.models.deck_profile import DeckProfile
from balatro_bot.domain.scoring.base import arm_reduce_hand_levels, flint_halve_hand_levels
from balatro_bot.cards import joker_key
from balatro_bot.domain.scoring.mouth_commit import choose_mouth_commit
from balatro_bot.domain.scoring.search import HandCandidate, best_hand
from balatro_bot.infrastructure.state_adapter import adapt_state
from balatro_bot.scaling import FINAL_HAND_JOKERS
from balatro_bot.strategy import Strategy, compute_strategy

if TYPE_CHECKING:
    from typing import Any

    from balatro_bot.domain.models.snapshot import Snapshot

# ---------------------------------------------------------------------------
# Round outlook thresholds (projected score / chips remaining)
# ---------------------------------------------------------------------------
_COMFORTABLE_RATIO = 1.5   # >= this → comfortable, blind is well in hand
_TIGHT_RATIO = 0.9         # >= this → tight, every hand matters

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
    hand_levels: dict
    jokers: list[dict]
    best: HandCandidate | None
    money: int
    ante: int
    round_num: int
    min_cards: int
    strategy: Strategy
    deck_cards: list[dict]
    deck_profile: DeckProfile = field(default_factory=DeckProfile)
    mouth_locked_hand: str | None = None
    committed_hand_type: str | None = None
    score_discount: float = 1.0
    forced_card_idx: int | None = None
    ancient_suit: str | None = None
    idol_rank: str | None = None
    idol_suit: str | None = None
    eye_used_hands: set[str] | None = None
    scoring_suit: str | None = None
    debuffed_suit: str | None = None
    ox_most_played: str | None = None
    best_as_finisher: HandCandidate | None = None

    @property
    def card_protection(self):
        """Build the CardProtection view from this round's strategy + state."""
        return self.strategy.card_protection(
            jokers=self.jokers,
            idol_rank=self.idol_rank,
            idol_suit=self.idol_suit,
            scoring_suit=self.scoring_suit,
            debuffed_suit=self.debuffed_suit,
            discards_left=self.discards_left,
            heavy_debuff=(self.blind_name == "The Pillar"),
        )

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
                if hasattr(hand_data, "get") and hand_data.get("played_this_round", 0) > 0:
                    mouth_locked_hand = hand_name
                    break

        eye_used_hands = None
        if blind_name == "The Eye" and not boss_disabled:
            eye_used_hands = {
                ht for ht, data in hand_levels.items()
                if hasattr(data, "get") and data.get("played_this_round", 0) > 0
            }

        scoring_suit = None
        debuffed_suit = None
        if not boss_disabled:
            if blind_name == "The Head":
                scoring_suit = "H"
            elif blind_name == "The Club":
                scoring_suit = "C"
            elif blind_name == "The Window":
                scoring_suit = "D"
            elif blind_name == "The Goad":
                debuffed_suit = "S"

        chips_scored = snapshot.round.chips
        hands_left = snapshot.round.hands_left
        discards_left = snapshot.round.discards_left

        # Crimson Heart: one random joker is debuffed each hand.  The API
        # reports which joker is currently debuffed, and the scoring pipeline
        # (score_hand / score_hand_detailed) already skips debuffed jokers via
        # is_joker_debuffed().  So ctx.best.total already reflects the penalty.
        # No additional score_discount is needed — applying one double-counts.
        score_discount = 1.0

        forced_card_idx = None
        if blind_name == "Cerulean Bell" and not boss_disabled:
            for i, c in enumerate(hand_cards):
                if c.state.highlight:
                    forced_card_idx = i
                    break

        ancient_suit = snapshot.round.ancient_suit
        idol_rank = snapshot.round.idol_rank
        idol_suit = snapshot.round.idol_suit
        ox_most_played = snapshot.round.most_played_poker_hand if blind_name == "The Ox" and not boss_disabled else None
        strat = compute_strategy(jokers, hand_levels)
        deck_profile = DeckProfile.from_cards(list(deck_cards) + list(hand_cards))
        # When boss is disabled (Luchador sold), clear blind_name for scoring
        # so boss-specific scoring effects (The Tooth, The Ox, etc.) don't fire.
        effective_blind = "" if boss_disabled else blind_name

        # The Mouth pre-lock: pick the hand type with best round-total EV
        # before the first play locks it. Once locked, mouth_locked_hand wins.
        committed_hand_type = None
        if (effective_blind == "The Mouth"
                and mouth_locked_hand is None
                and hands_left > 0):
            committed_hand_type = choose_mouth_commit(
                hand_cards, hand_levels,
                jokers=jokers, joker_limit=snapshot.joker_limit,
                hands_left=hands_left, discards_left=discards_left,
                money=money,
                deck_cards=deck_cards, deck_profile=deck_profile,
                ancient_suit=ancient_suit,
                idol_rank=idol_rank, idol_suit=idol_suit,
                forced_card_idx=forced_card_idx,
                blind_name=effective_blind,
                ox_most_played=ox_most_played,
            )

        effective_commit = mouth_locked_hand or committed_hand_type

        best = best_hand(
            hand_cards, hand_levels,
            min_select=min_cards, jokers=jokers,
            money=money, discards_left=discards_left,
            hands_left=hands_left,
            joker_limit=snapshot.joker_limit,
            required_hand=effective_commit,
            required_card_indices={forced_card_idx} if forced_card_idx is not None else None,
            ancient_suit=ancient_suit,
            excluded_hands=eye_used_hands,
            deck_cards=deck_cards,
            blind_name=effective_blind,
            ox_most_played=ox_most_played,
            idol_rank=idol_rank,
            idol_suit=idol_suit,
        )

        # Score best hand as if it were the final hand (hands_left=1) so the
        # planner can see the Acrobat x3 / Dusk retrigger value.
        joker_keys_set = {joker_key(j) for j in jokers}
        has_final_hand = bool(joker_keys_set & FINAL_HAND_JOKERS) and hands_left > 1
        best_as_finisher = (
            best_hand(
                hand_cards, hand_levels,
                min_select=min_cards, jokers=jokers,
                money=money, discards_left=discards_left,
                hands_left=1,
                joker_limit=snapshot.joker_limit,
                required_hand=effective_commit,
                required_card_indices={forced_card_idx} if forced_card_idx is not None else None,
                ancient_suit=ancient_suit,
                excluded_hands=eye_used_hands,
                deck_cards=deck_cards,
                blind_name=effective_blind,
                ox_most_played=ox_most_played,
            )
            if has_final_hand else None
        )

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
            best=best,
            mouth_locked_hand=mouth_locked_hand,
            committed_hand_type=committed_hand_type,
            score_discount=score_discount,
            forced_card_idx=forced_card_idx,
            ancient_suit=ancient_suit,
            idol_rank=idol_rank,
            idol_suit=idol_suit,
            eye_used_hands=eye_used_hands,
            scoring_suit=scoring_suit,
            debuffed_suit=debuffed_suit,
            ox_most_played=ox_most_played,
            money=money,
            ante=snapshot.ante,
            round_num=snapshot.round_num,
            min_cards=min_cards,
            strategy=strat,
            deck_cards=deck_cards,
            deck_profile=deck_profile,
            best_as_finisher=best_as_finisher,
        )
