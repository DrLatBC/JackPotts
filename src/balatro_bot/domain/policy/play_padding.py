"""Pad a play with junk cards to cycle the deck."""

from __future__ import annotations

from typing import TYPE_CHECKING

from balatro_bot.cards import card_rank, joker_key, rank_value
from balatro_bot.constants import FEWER_CARDS_JOKERS, ALL_SCORE_JOKERS, EXACT_4_JOKERS
from balatro_bot.domain.scoring.classify import classify_hand
from balatro_bot.domain.scoring.search import cards_not_in

if TYPE_CHECKING:
    from balatro_bot.strategy import CardProtection, Strategy


def pad_with_junk(
    card_indices: list[int],
    hand_cards: list[dict],
    jokers: list[dict],
    intended_hand: str = "",
    max_cards: int = 5,
    strategy: Strategy | None = None,
    scoring_suit: str | None = None,
    protection: CardProtection | None = None,
) -> list[int]:
    """Pad a hand with junk cards for free deck cycling.

    Uses the same strategic discard ordering as the discard system —
    cards we'd most want to discard are the first ones cycled out via play.
    If intended_hand is provided, each candidate is checked to ensure it
    doesn't change the hand classification.
    """
    jk = {joker_key(j) for j in jokers}
    n = len(card_indices)

    if jk & FEWER_CARDS_JOKERS and n <= 3:
        return card_indices

    if jk & EXACT_4_JOKERS:
        target = 4
    else:
        target = max_cards

    if n >= target:
        return card_indices

    four_fingers = "j_four_fingers" in jk
    shortcut = "j_shortcut" in jk
    smeared = "j_smeared" in jk
    # Splash makes every played card score, so classification shifts and
    # high-rank "junk" don't erase chip contribution — skip those guards.
    all_score = bool(jk & ALL_SCORE_JOKERS)

    if protection is None and strategy is not None:
        protection = strategy.card_protection(jokers=jokers, scoring_suit=scoring_suit)

    # cards_not_in returns non-keep indices sorted worst-first (discard priority)
    junk_priority = cards_not_in(
        hand_cards, set(card_indices),
        protection=protection,
    )

    # For High Card, junk must not outrank the scoring card or it becomes
    # the new scoring card (game picks highest rank, leftmost on ties).
    max_junk_rank: int | None = None
    if intended_hand == "High Card" and card_indices and not all_score:
        scoring_rank = rank_value(card_rank(hand_cards[card_indices[0]]) or "2")
        max_junk_rank = scoring_rank

    padded = list(card_indices)
    for i in junk_priority:
        if len(padded) >= target:
            break
        if max_junk_rank is not None:
            rv = rank_value(card_rank(hand_cards[i]) or "2")
            if rv > max_junk_rank:
                continue
        if intended_hand:
            test_cards = [hand_cards[j] for j in padded] + [hand_cards[i]]
            if classify_hand(test_cards, four_fingers, shortcut, smeared) != intended_hand:
                continue
        padded.append(i)

    return padded
