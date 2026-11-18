"""RoundContext — pre-computed facts about the current round for rules to use."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from balatro_bot.domain.models.deck_profile import DeckProfile
from balatro_bot.domain.scoring.base import arm_reduce_hand_levels, flint_halve_hand_levels
from balatro_bot.cards import joker_key
from balatro_bot.domain.scoring.mouth_commit import choose_mouth_commit
from balatro_bot.domain.policy.round_search import RoundSearch
from balatro_bot.domain.scoring.search import HandCandidate
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


def _hand_affinity(strategy: Strategy | None) -> dict[str, float] | None:
    return dict(strategy.preferred_hands) if strategy and strategy.preferred_hands else None


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
    best_without_forced: HandCandidate | None = None
    joker_limit: int = 5
    search: RoundSearch | None = None

    def __post_init__(self) -> None:
        if self.search is None:
            self.search = RoundSearch(
                hand_cards=self.hand_cards,
                hand_levels=self.hand_levels,
                jokers=self.jokers,
                joker_limit=self.joker_limit,
                money=self.money,
                hands_left=self.hands_left,
                discards_left=self.discards_left,
                min_cards=self.min_cards,
                blind_name=self.blind_name,
                mouth_hand=self.mouth_locked_hand or self.committed_hand_type,
                forced_card=(None if self.forced_card_idx is None
                             else self.hand_cards[self.forced_card_idx]),
                eye_used=self.eye_used_hands,
                ancient_suit=self.ancient_suit,
                idol_rank=self.idol_rank,
                idol_suit=self.idol_suit,
                ox_most_played=self.ox_most_played,
                deck_cards=self.deck_cards,
                hand_affinity=_hand_affinity(self.strategy),
                strategy=self.strategy,
                scoring_suit=self.scoring_suit,
                debuffed_suit=self.debuffed_suit,
            )

    @property
    def kept_best(self) -> HandCandidate | None:
        """The best hand still in hand after a play or discard spent on something else.
        Cerulean Bell's forced card leaves with every play and discard, so there it is
        the best hand that does not use that card."""
        return self.best if self.forced_card_idx is None else self.best_without_forced

    @property
    def kept_indices(self) -> set[int]:
        """Hand positions of ``kept_best``: the cards to hold back while cycling junk."""
        kept = self.kept_best
        return set(kept.card_indices) if kept else set()

    @property
    def required_cards(self) -> set[int] | None:
        """The hand positions every play must contain, for a hand search."""
        return None if self.forced_card_idx is None else {self.forced_card_idx}

    def with_forced(self, indices: list[int], limit: int = 5) -> list[int]:
        """The cards a play or discard of ``indices`` really selects: Cerulean Bell's
        forced card first, then the given cards in order, ``limit`` at most."""
        forced = self.forced_card_idx
        if forced is None:
            return list(indices)[:limit]
        return ([forced] + [i for i in indices if i != forced])[:limit]

    def discard_with_forced(self, indices: list[int], count: int) -> list[int]:
        """A discard of the first ``count`` of ``indices``. The forced card goes out on
        top of them, within the game's limit of five cards."""
        if self.forced_card_idx is None:
            return list(indices)[:count]
        return self.with_forced(indices, min(count + 1, 5))

    @property
    def card_protection(self):
        """Build the CardProtection view from this round's strategy + state."""
        return self.search.card_protection()

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
        deck_profile = DeckProfile.from_cards(list(deck_cards) + list(hand_cards))
        strat = compute_strategy(jokers, hand_levels, deck_profile=deck_profile)
        # When boss is disabled (Luchador sold), clear blind_name for scoring
        # so boss-specific scoring effects (The Tooth, The Ox, etc.) don't fire.
        effective_blind = "" if boss_disabled else blind_name

        search = RoundSearch(
            hand_cards=hand_cards,
            hand_levels=hand_levels,
            jokers=jokers,
            joker_limit=snapshot.joker_limit,
            money=money,
            hands_left=hands_left,
            discards_left=discards_left,
            min_cards=min_cards,
            blind_name=effective_blind,
            forced_card=None if forced_card_idx is None else hand_cards[forced_card_idx],
            eye_used=eye_used_hands,
            ancient_suit=ancient_suit,
            idol_rank=idol_rank,
            idol_suit=idol_suit,
            ox_most_played=ox_most_played,
            deck_cards=deck_cards,
            hand_affinity=_hand_affinity(strat),
            strategy=strat,
            scoring_suit=scoring_suit,
            debuffed_suit=debuffed_suit,
        )

        # The Mouth pre-lock: pick the hand type with best round-total EV
        # before the first play locks it. Once locked, mouth_locked_hand wins.
        committed_hand_type = None
        if (effective_blind == "The Mouth"
                and mouth_locked_hand is None
                and hands_left > 0):
            committed_hand_type = choose_mouth_commit(search, deck_profile)

        search = search.locked_to(mouth_locked_hand or committed_hand_type)
        best = search.best()

        # Score best hand as if it were the final hand (hands_left=1) so the
        # planner can see the Acrobat x3 / Dusk retrigger value.
        joker_keys_set = {joker_key(j) for j in jokers}
        has_final_hand = bool(joker_keys_set & FINAL_HAND_JOKERS) and hands_left > 1
        best_as_finisher = search.best(hands_left=1) if has_final_hand else None

        best_without_forced = None
        if forced_card_idx is not None:
            others = [i for i in range(len(hand_cards)) if i != forced_card_idx]
            best_without_forced = search.best([hand_cards[i] for i in others])
            if best_without_forced is not None:
                best_without_forced.card_indices = [others[i] for i in best_without_forced.card_indices]

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
            best_without_forced=best_without_forced,
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
            joker_limit=snapshot.joker_limit,
            search=search,
        )
