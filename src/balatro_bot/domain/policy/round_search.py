"""Round search: hand search with the round's rules already in force."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from balatro_bot.domain.policy.play_padding import pad_with_junk
from balatro_bot.domain.scoring import estimate
from balatro_bot.domain.scoring.play_order import sort_play_order
from balatro_bot.domain.scoring.search import HandCandidate, best_hand, enumerate_hands

if TYPE_CHECKING:
    from collections.abc import Iterable

    from balatro_bot.strategy import CardProtection, Strategy

__all__ = ["RoundSearch"]


@dataclass(frozen=True)
class RoundSearch:
    """The round's rules for every hand search, play order and junk padding.

    A search names only what differs from the round: other cards, fewer hands
    or discards, adjusted jokers, a target hand type or an extra excluded type.
    The one round rule a search can lift is The Mouth's lock (``ignore_mouth``),
    for when the locked type can't be formed and something must be played.
    Card positions in results refer to the cards searched (the hand by default).
    """

    hand_cards: list
    hand_levels: dict
    jokers: list
    joker_limit: int = 5
    money: int = 0
    hands_left: int = 1
    discards_left: int = 0
    min_cards: int = 1
    blind_name: str = ""
    mouth_hand: str | None = None
    forced_card: object | None = None
    eye_used: frozenset[str] | set[str] | None = None
    ancient_suit: str | None = None
    idol_rank: str | None = None
    idol_suit: str | None = None
    ox_most_played: str | None = None
    deck_cards: list | None = None
    hand_affinity: dict[str, float] | None = None
    strategy: Strategy | None = None
    scoring_suit: str | None = None
    debuffed_suit: str | None = None

    def locked_to(self, hand_type: str | None) -> RoundSearch:
        """This search with The Mouth's hand type in force."""
        return self if hand_type == self.mouth_hand else replace(self, mouth_hand=hand_type)

    def _params(
        self,
        cards: list | None,
        hands_left: int | None,
        discards_left: int | None,
        jokers: list | None,
        target: str | None,
        exclude: Iterable[str] | None,
        ignore_mouth: bool,
    ) -> dict:
        hand = self.hand_cards if cards is None else cards
        forced = None
        if self.forced_card is not None:
            # The forced card is matched by identity: it may sit at any position, or be
            # absent, in the cards searched.
            forced = {i for i, c in enumerate(hand) if c is self.forced_card} or None
        excluded = self.eye_used
        if exclude:
            excluded = set(excluded or ()) | set(exclude)
        if target is not None:
            required = target
        else:
            required = None if ignore_mouth else self.mouth_hand
        return dict(
            hand_cards=hand,
            hand_levels=self.hand_levels,
            min_select=self.min_cards,
            jokers=self.jokers if jokers is None else jokers,
            money=self.money,
            discards_left=self.discards_left if discards_left is None else discards_left,
            hands_left=self.hands_left if hands_left is None else hands_left,
            joker_limit=self.joker_limit,
            required_hand=required,
            required_card_indices=forced,
            ancient_suit=self.ancient_suit,
            excluded_hands=excluded,
            deck_cards=self.deck_cards,
            blind_name=self.blind_name,
            ox_most_played=self.ox_most_played,
            idol_rank=self.idol_rank,
            idol_suit=self.idol_suit,
            hand_affinity=self.hand_affinity,
        )

    def enumerate(
        self,
        cards: list | None = None,
        *,
        hands_left: int | None = None,
        discards_left: int | None = None,
        jokers: list | None = None,
        target: str | None = None,
        exclude: Iterable[str] | None = None,
        ignore_mouth: bool = False,
    ) -> list[HandCandidate]:
        """Every allowed play, best first."""
        return enumerate_hands(**self._params(
            cards, hands_left, discards_left, jokers, target, exclude, ignore_mouth))

    def best(
        self,
        cards: list | None = None,
        *,
        hands_left: int | None = None,
        discards_left: int | None = None,
        jokers: list | None = None,
        target: str | None = None,
        exclude: Iterable[str] | None = None,
        ignore_mouth: bool = False,
    ) -> HandCandidate | None:
        """The best allowed play, or None when no play is allowed."""
        return best_hand(**self._params(
            cards, hands_left, discards_left, jokers, target, exclude, ignore_mouth))

    def best_totals(
        self,
        card_lists: list[list],
        *,
        hands_left: int | None = None,
        discards_left: int | None = None,
        jokers: list | None = None,
        target: str | None = None,
        exclude: Iterable[str] | None = None,
        ignore_mouth: bool = False,
    ) -> list[int]:
        """The best allowed play's total for each card list (0 where none is allowed)."""
        p = self._params(None, hands_left, discards_left, jokers, target, exclude, ignore_mouth)
        forced_present = self.forced_card is not None and any(
            c is self.forced_card for cards in card_lists for c in cards)
        # The kernel's batch call applies no play limits; with any in force each list
        # is searched on its own.
        if (estimate.USE_KERNEL and p["min_select"] <= 1 and not p["required_hand"]
                and not p["excluded_hands"] and not forced_present):
            from balatro_bot.domain.scoring.kernel import KernelScorer

            scorer = KernelScorer(
                p["hand_levels"], p["jokers"],
                money=p["money"], discards_left=p["discards_left"], hands_left=p["hands_left"],
                joker_limit=p["joker_limit"], ancient_suit=p["ancient_suit"],
                blind_name=p["blind_name"], ox_most_played=p["ox_most_played"],
                idol_rank=p["idol_rank"], idol_suit=p["idol_suit"],
            )
            return scorer.best_totals(card_lists)
        totals = []
        for cards in card_lists:
            found = self.best(cards, hands_left=p["hands_left"], discards_left=p["discards_left"],
                              jokers=p["jokers"], target=target, exclude=exclude,
                              ignore_mouth=ignore_mouth)
            totals.append(found.total if found else 0)
        return totals

    def order(self, indices: list[int]) -> list[int]:
        """The order to play the hand cards at ``indices`` in."""
        return sort_play_order(
            indices, self.hand_cards, self.jokers, self.strategy,
            idol_rank=self.idol_rank, idol_suit=self.idol_suit, ancient_suit=self.ancient_suit,
        )

    def play(self, indices: list[int], hand_name: str) -> list[int]:
        """A play of ``indices`` padded with junk and put in play order."""
        padded = pad_with_junk(
            indices, self.hand_cards, self.jokers, hand_name, protection=self.card_protection(),
        )
        return self.order(padded)

    def card_protection(self) -> CardProtection:
        """How strongly each card should be kept this round."""
        return self.strategy.card_protection(
            jokers=self.jokers,
            idol_rank=self.idol_rank,
            idol_suit=self.idol_suit,
            scoring_suit=self.scoring_suit,
            debuffed_suit=self.debuffed_suit,
            discards_left=self.discards_left,
            heavy_debuff=(self.blind_name == "The Pillar"),
        )
