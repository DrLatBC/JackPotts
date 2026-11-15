"""ScoreContext dataclass and helper functions for joker effect evaluation."""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import TYPE_CHECKING

from balatro_bot.cards import card_facts, card_rank, card_suits, is_debuffed
from balatro_bot.constants import FACE_RANKS
from balatro_bot.domain.models.card import Card
from balatro_bot.domain.models.joker import Joker
from balatro_bot.joker_effects.roster import Roster, roster

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
    idol_rank: str | None = None
    idol_suit: str | None = None
    # Oops! All 6s flag — doubles probability-based trigger rates during sampling.
    oops: bool = False
    # Monte Carlo RNG — when present, stochastic jokers (Misprint, Lucky Cat,
    # Bloodstone) draw actual outcomes instead of using expected-value shortcuts.
    rng: random.Random | None = None
    roster: Roster | None = None  # filled from jokers when not passed
    # _played_rank_kinds' last answer: (the played_cards list it counted, top, pairs).
    played_kinds: tuple | None = None

    def __post_init__(self) -> None:
        if self.roster is None:
            self.roster = roster(self.jokers)


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


_RANK_SUB_HANDS = frozenset({"Pair", "Two Pair", "Three of a Kind", "Four of a Kind"})


def _played_rank_kinds(ctx: ScoreContext) -> tuple[int, int]:
    """Among the played cards: (most cards of one rank, how many ranks appear twice or more).

    Several hand-type jokers ask about the same play, so the answer is kept
    until ctx.played_cards is a different list.
    """
    memo = ctx.played_kinds
    if memo is not None and memo[0] is ctx.played_cards:
        return memo[1], memo[2]
    counts: dict[str, int] = {}
    for c in ctx.played_cards:
        rank = card_rank(c)
        if rank:
            counts[rank] = counts.get(rank, 0) + 1
    top = max(counts.values(), default=0)
    pairs = sum(1 for v in counts.values() if v >= 2)
    ctx.played_kinds = (ctx.played_cards, top, pairs)
    return top, pairs


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
    if not _RANK_SUB_HANDS.isdisjoint(hand_types):
        top, pairs = _played_rank_kinds(ctx)
        if "Pair" in hand_types and top >= 2:
            return True
        if "Two Pair" in hand_types and pairs >= 2:
            return True
        if "Three of a Kind" in hand_types and top >= 3:
            return True
        if "Four of a Kind" in hand_types and top >= 4:
            return True

    return False


def _noop(ctx: ScoreContext, j: dict) -> None:
    pass


def retrigger_jokers(lineup: Roster) -> tuple[int, int, int, int, int]:
    """How many of each retrigger joker are in effect, copies by Blueprint and
    Brainstorm included: (hack, sock_and_buskin, hanging_chad, dusk, seltzer)."""
    counts = lineup.derived.get("retrigger")
    if counts is None:
        keys = [k for k, _ in lineup.resolved]
        counts = lineup.derived["retrigger"] = (
            keys.count("j_hack"),
            keys.count("j_sock_and_buskin"),
            keys.count("j_hanging_chad"),
            keys.count("j_dusk"),
            keys.count("j_selzer"),  # game API key is j_selzer (no second 't')
        )
    return counts


def first_played_scoring(ctx: ScoreContext) -> CardLike | None:
    """The first played card that scores: the one Hanging Chad retriggers.

    The game uses played order, not the bot's internal scoring order
    (which reorders e.g. trips before pairs in Full House).
    """
    scoring_ids = {id(s) for s in ctx.scoring_cards}
    return next((c for c in ctx.played_cards if id(c) in scoring_ids), None)


def retrigger_count(card: Card | dict, ctx: ScoreContext) -> int:
    facts = card_facts(card)
    if facts.debuffed:
        return 1
    hack, sock, chad, dusk, seltzer = retrigger_jokers(ctx.roster)
    count = 1 + seltzer
    if facts.hack_rank:
        count += hack
    if facts.face or ctx.pareidolia:
        count += sock
    if chad and card is first_played_scoring(ctx):
        count += 2 * chad
    if ctx.hands_left == 1:
        count += dusk
    if facts.red_seal:
        count += 1
    return count
