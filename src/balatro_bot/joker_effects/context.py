"""ScoreContext dataclass and helper functions for joker effect evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from balatro_bot.cards import card_rank, card_suits, is_debuffed, joker_key, _modifier
from balatro_bot.constants import FACE_RANKS
from balatro_bot.domain.models.card import Card
from balatro_bot.domain.models.joker import Joker

if TYPE_CHECKING:
    from typing import Any

    CardLike = Card | dict[str, Any]
    JokerLike = Joker | dict[str, Any]


@dataclass
class ScoreContext:
    chips: int
    mult: float
    hand_name: str
    scoring_cards: list[CardLike]
    played_cards: list[CardLike]
    held_cards: list[CardLike]
    hand_levels: dict
    jokers: list[JokerLike]
    money: int
    discards_left: int
    hands_left: int
    joker_limit: int = 5
    deck_count: int = 0
    deck_cards: list[CardLike] | None = None
    pareidolia: bool = False
    smeared: bool = False
    ancient_suit: str | None = None
    vampire_xmult: float | None = None  # Pre-computed Vampire xmult (after before-phase increment)
    blind_name: str = ""


def _count_suit_in_scoring(ctx: ScoreContext, suit: str) -> int:
    return sum(
        retrigger_count(c, ctx)
        for c in ctx.scoring_cards
        if not is_debuffed(c) and suit in card_suits(c, smeared=ctx.smeared)
    )


def _count_face_in_scoring(ctx: ScoreContext) -> int:
    return sum(
        retrigger_count(c, ctx)
        for c in ctx.scoring_cards
        if not is_debuffed(c) and (ctx.pareidolia or card_rank(c) in FACE_RANKS)
    )


def _hand_contains(ctx: ScoreContext, *hand_types: str) -> bool:
    """Check if the played hand 'contains' the given sub-hand type(s).

    In Balatro, jokers like Sly/Jolly/Droll fire when the hand *contains*
    a sub-hand, not just when the classified type matches.  A Flush with
    two same-rank cards contains a Pair; a Flush with two distinct pairs
    contains Two Pair; etc.  The game checks the actual cards for rank
    patterns, so we must too.
    """
    name = ctx.hand_name
    for ht in hand_types:
        if name == ht:
            return True

    # --- Hand-type hierarchy (classified name implies sub-types) ---
    if "Pair" in hand_types:
        if name in ("Pair", "Two Pair", "Three of a Kind", "Full House",
                     "Four of a Kind", "Five of a Kind", "Flush House", "Flush Five"):
            return True
    if "Three of a Kind" in hand_types:
        if name in ("Three of a Kind", "Full House", "Four of a Kind",
                     "Five of a Kind", "Flush House", "Flush Five"):
            return True
    if "Four of a Kind" in hand_types:
        if name in ("Four of a Kind", "Five of a Kind", "Flush Five"):
            return True
    if "Two Pair" in hand_types:
        if name in ("Two Pair", "Full House", "Flush House"):
            return True
    if "Straight" in hand_types:
        if name in ("Straight", "Straight Flush"):
            return True
    if "Flush" in hand_types:
        if name in ("Flush", "Straight Flush", "Flush House", "Flush Five"):
            return True

    # --- Card-based detection for rank sub-hands hidden in other types ---
    # A Flush can contain Pair/Two Pair/Trips/Quads from duplicate ranks.
    # Only check rank-based sub-hands; Flush/Straight are already covered above.
    _rank_types = {"Pair", "Two Pair", "Three of a Kind", "Four of a Kind"}
    if _rank_types & set(hand_types):
        from collections import Counter
        rank_counts = Counter(
            card_rank(c) for c in ctx.played_cards if card_rank(c)
        )
        counts = sorted(rank_counts.values(), reverse=True)
        if "Pair" in hand_types and counts and counts[0] >= 2:
            return True
        if "Two Pair" in hand_types and len([c for c in counts if c >= 2]) >= 2:
            return True
        if "Three of a Kind" in hand_types and counts and counts[0] >= 3:
            return True
        if "Four of a Kind" in hand_types and counts and counts[0] >= 4:
            return True

    return False


def _noop(ctx: ScoreContext, j: dict) -> None:
    pass


def retrigger_count(card: Card | dict, ctx: ScoreContext) -> int:
    if is_debuffed(card):
        return 1
    count = 1
    rank = card_rank(card)
    joker_keys = {joker_key(j) for j in ctx.jokers}

    if "j_hack" in joker_keys and rank in ("2", "3", "4", "5"):
        count += 1

    if "j_sock_and_buskin" in joker_keys and (rank in FACE_RANKS or ctx.pareidolia):
        count += 1

    if "j_hanging_chad" in joker_keys:
        # Hanging Chad retriggers the first PLAYED card that is used in scoring.
        # The game uses played order, not the bot's internal scoring order
        # (which reorders e.g. trips before pairs in Full House).
        first_played_scoring = next(
            (c for c in ctx.played_cards
             if any(c is s for s in ctx.scoring_cards)), None
        )
        if first_played_scoring is not None and card is first_played_scoring:
            count += 2

    if "j_dusk" in joker_keys and ctx.hands_left == 1:
        count += 1

    if "j_selzer" in joker_keys:  # game API key is j_selzer (no second 't')
        count += 1

    if _modifier(card).get("seal") == "RED":
        count += 1

    return count
