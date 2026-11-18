"""Play order: the cards a policy chose, played in the bot's left-to-right order.

Played cards score left to right, and a policy only says which cards to play. Once
the cards are chosen their best order is a calculation, so it is done for the policy:
the bot's own rule (``balatro_bot.domain.scoring.play_order``) orders them, and the
hand is reordered on the game before the play, the way ``bot.py`` does it. Jackdaw's
environment can only move a card one place at a time, at most ten moves in a row, so
the reorder is applied to the environment's game directly.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "sim" / "jackdaw_adapter"))

from balatro_bot.domain.models.deck_profile import DeckProfile  # noqa: E402
from balatro_bot.domain.scoring.play_order import hand_order_for_play, sort_play_order  # noqa: E402
from balatro_bot.infrastructure.state_adapter import adapt_state  # noqa: E402
from balatro_bot.strategy import compute_strategy  # noqa: E402
from fork_view import augment  # noqa: E402
from jackdaw.engine import actions as A  # noqa: E402

from rl.convert import left_moves  # noqa: E402

# ``sort_play_order`` gives plain hand order unless the lineup puts a card first
# (Hanging Chad, Photograph, DNA), a joker multiplies a scored card's mult (Triboulet,
# Ancient Joker, Bloodstone, The Idol; Photograph again), a copier may copy one of
# those, or a chosen card is Glass or Polychrome. Without any of them the bot's whole view need not
# be rebuilt. ``rl/tests/test_ordering.py`` holds this equal to the full rule on
# recorded plays.
ORDER_JOKERS = frozenset({
    "j_hanging_chad", "j_photograph", "j_dna", "j_triboulet", "j_ancient", "j_bloodstone",
    "j_idol", "j_blueprint", "j_brainstorm",
})
ORDER_CARDS = frozenset({"m_glass"})


def order_can_change(game, cards) -> bool:
    """Whether the bot's rule might play ``cards`` in other than hand order."""
    if len(cards) < 2:
        return False
    if any(j.key in ORDER_JOKERS for j in game.jokers):
        return True
    hand = game.hand
    return any(hand[c].key in ORDER_CARDS or hand[c].edition == "polychrome" for c in cards)


def bot_state(game) -> dict:
    """The game state as the bot receives it from the simulator."""
    return augment(game.bot_view(), game.to_dict())


def bot_order(game, cards) -> list[int]:
    """The hand positions ``cards`` in the order the bot would play them."""
    snapshot = adapt_state(bot_state(game))
    hand, jokers = snapshot.hand_cards, snapshot.jokers
    strategy = compute_strategy(
        jokers, snapshot.hand_levels,
        deck_profile=DeckProfile.from_cards(list(snapshot.deck_cards) + list(hand)),
    )
    # The bot hands its rule the scoring cards first and the padding after, and the rule
    # breaks ties toward the earlier card.
    found = game.scoring_cards(sorted(cards))
    scoring = sorted(found[1]) if found else []
    return sort_play_order(
        scoring + [c for c in sorted(cards) if c not in scoring], hand, jokers, strategy,
        idol_rank=snapshot.round.idol_rank, idol_suit=snapshot.round.idol_suit,
        ancient_suit=snapshot.round.ancient_suit,
    )


def put_in_order(game, cards) -> tuple[int, ...]:
    """Reorder the game's hand so ``cards`` sit in the bot's play order among their own
    slots, every other card where it was.
    Returns the hand positions to play: the cards as given when they need no reorder."""
    if not order_can_change(game, cards):
        return tuple(sorted(cards))
    order = bot_order(game, cards)
    if order == sorted(order):
        return tuple(order)
    for at in left_moves(hand_order_for_play(order, len(game.hand))):
        game.step(A.SwapHandLeft(at))
    return tuple(sorted(order))
