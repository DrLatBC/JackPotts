"""Joker-aware chase ranking tests.

Demonstrates the payoff of the MC unification: chase EV now reflects joker
effects that the old static ranker ignored (Bloodstone probabilistic hearts,
Triboulet K/Q xmult, Steel Joker enhancements, The Idol's round target card).
"""

import random

from balatro_bot.context import RoundContext
from balatro_bot.strategy import compute_strategy
from balatro_bot.domain.scoring.search import best_hand
from balatro_bot.domain.policy.discard_policy import _expected_play_value
from tests.conftest import card, joker


def _deck_excluding(hand_cards):
    keys = {(c.value.rank, c.value.suit) for c in hand_cards}
    return [card(r, s) for r in "23456789TJQKA" for s in "HDCS" if (r, s) not in keys]


def _ctx(hand, jokers_list=None, deck=None, idol_rank=None, idol_suit=None):
    jokers_list = jokers_list or []
    if deck is None:
        deck = _deck_excluding(hand)
    ctx = RoundContext(
        blind_score=5000, blind_name="", chips_scored=0,
        chips_remaining=5000, hands_left=4, discards_left=3,
        hand_cards=hand, hand_levels={}, jokers=jokers_list,
        best=best_hand(hand, jokers=jokers_list),
        money=5, ante=1, round_num=1, min_cards=1,
        strategy=compute_strategy(jokers_list, {}),
        deck_cards=deck,
        idol_rank=idol_rank, idol_suit=idol_suit,
    )
    return ctx


class TestIdolAwareChase:
    """The Idol's round target card should boost chases that preserve matching cards."""

    def test_keep_idol_target_beats_discarding_it(self):
        """Two identical chases; one keeps the Idol target (A♠), the other discards it."""
        hand = [card("A", "S"), card("A", "D"),   # pair of Aces
                card("9", "H"), card("5", "C"), card("3", "D")]
        idol_joker = joker("j_idol")
        # Idol target = Ace of Spades — A♠ scoring should give X2 mult
        ctx = _ctx(hand, [idol_joker], idol_rank="A", idol_suit="S")

        random.seed(42)
        keep_as = _expected_play_value([0, 1], ctx)   # keep both Aces including A♠
        random.seed(42)
        drop_as = _expected_play_value([1, 2, 3, 4], ctx)   # discard A♠, keep A♦

        assert keep_as > drop_as, (
            f"Keeping A♠ (idol target) EV {keep_as:.0f} should beat discarding it EV {drop_as:.0f}"
        )


class TestBloodstoneAwareChase:
    """Bloodstone's heart bonus should favor chases that load up on hearts."""

    def test_heart_flush_chase_beats_spade_flush_chase(self):
        """With Bloodstone, an all-hearts flush should EV-dominate an all-spades flush."""
        # 4 hearts + 4 spades (8-card hand), discard 4 to chase either flush
        hand = [card("A", "H"), card("K", "H"), card("Q", "H"), card("J", "H"),
                card("A", "S"), card("K", "S"), card("Q", "S"), card("J", "S")]
        bloodstone = joker("j_bloodstone")
        ctx = _ctx(hand, [bloodstone])

        random.seed(42)
        hearts_ev = _expected_play_value([0, 1, 2, 3], ctx)   # keep hearts
        random.seed(42)
        spades_ev = _expected_play_value([4, 5, 6, 7], ctx)   # keep spades

        assert hearts_ev > spades_ev, (
            f"Heart-flush chase EV {hearts_ev:.0f} (Bloodstone proc) should beat "
            f"Spade-flush chase EV {spades_ev:.0f}"
        )


class TestTribouletAwareChase:
    """Triboulet's K/Q xmult should favor chases that preserve K/Q cards."""

    def test_keep_kings_beats_keep_low_pair(self):
        """With Triboulet, keeping Kings for a flush chase should beat keeping a 3-pair."""
        hand = [card("K", "H"), card("K", "D"),            # pair of Kings (x4 xmult each)
                card("3", "S"), card("3", "C"),            # pair of 3s (no Triboulet bonus)
                card("9", "H"), card("5", "H"), card("7", "H"), card("2", "H")]
        triboulet = joker("j_triboulet")
        ctx = _ctx(hand, [triboulet])

        random.seed(42)
        keep_kings = _expected_play_value([0, 1], ctx)        # keep Kings
        random.seed(42)
        keep_threes = _expected_play_value([2, 3], ctx)       # keep 3s

        assert keep_kings > keep_threes, (
            f"Keeping Kings (Triboulet xmult) EV {keep_kings:.0f} should beat "
            f"keeping 3s EV {keep_threes:.0f}"
        )
