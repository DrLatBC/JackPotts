"""Cerulean Bell forces one hand card into every play and every discard. Whatever the
bot plays or discards has to contain it, and whatever it plans to keep cannot."""

from __future__ import annotations

import random
from dataclasses import replace

from balatro_bot.actions import DiscardCards, PlayCards
from balatro_bot.context import RoundContext
from balatro_bot.domain.models.card import CardState
from balatro_bot.domain.policy.discard_policy import choose_discard
from balatro_bot.domain.policy.hand_sequencing import (
    _dna_solo,
    _execute_setup,
    _play_junk,
    _sixth_sense_solo,
    _trading_first_discard,
    build_round_plan,
    execute_plan_step,
)
from balatro_bot.domain.policy.play_policy import (
    choose_best_available,
    choose_high_value_play,
    choose_milk_play,
    choose_winning_play,
)
from balatro_bot.domain.scoring.classify import classify_hand
from balatro_bot.domain.scoring.search import discard_candidates
from tests.conftest import card, joker

HAND_LEVELS = {
    name: {"chips": chips, "mult": mult, "level": 1}
    for name, chips, mult in [
        ("High Card", 5, 1), ("Pair", 10, 2), ("Two Pair", 20, 2), ("Three of a Kind", 30, 3),
        ("Straight", 30, 4), ("Flush", 35, 4), ("Full House", 40, 4), ("Four of a Kind", 60, 7),
        ("Straight Flush", 100, 8),
    ]
}
RANKS = "23456789TJQKA"
SUITS = "HDCS"


def _ctx(hand, forced=None, jokers=(), score=300, hands_left=4, discards_left=3, bell=True,
         deck=None) -> RoundContext:
    """A round context; ``forced`` is the hand position Cerulean Bell forces."""
    cards = list(hand)
    if forced is not None:
        cards[forced] = replace(cards[forced], state=CardState(highlight=True))
    blind = ({"key": "bl_final_bell", "name": "Cerulean Bell"} if bell
             else {"key": "bl_big", "name": "Big Blind"})
    deck = deck if deck is not None else [card(r, s) for r in "2468TQ" for s in SUITS]
    return RoundContext.from_state({
        "hand": {"cards": cards},
        "jokers": {"cards": list(jokers), "limit": 5},
        "cards": {"cards": deck, "count": len(deck)},
        "round": {"chips": 0, "hands_left": hands_left, "discards_left": discards_left},
        "blinds": {"boss": {**blind, "status": "CURRENT", "score": score}},
        "hands": HAND_LEVELS,
        "money": 10, "ante_num": 8, "round_num": 24,
    })


def _cards(action) -> list[int]:
    return action.to_rpc()[1]["cards"]


def _respects_bell(action, ctx) -> bool:
    if not isinstance(action, (PlayCards, DiscardCards)):
        return True
    cards = _cards(action)
    return ctx.forced_card_idx in cards and len(cards) <= 5 and len(set(cards)) == len(cards)


# Kings at 0 and 1, junk after. Forcing a King makes the forced card part of the best hand.
PAIR_HAND = [card("K", "H"), card("K", "D"), card("9", "C"), card("7", "S"),
             card("5", "H"), card("4", "D"), card("3", "C"), card("2", "S")]


class TestContext:
    def test_without_a_forced_card_nothing_changes(self):
        ctx = _ctx(PAIR_HAND, bell=False)
        assert ctx.forced_card_idx is None and ctx.required_cards is None
        assert ctx.kept_best is ctx.best
        assert ctx.with_forced([3, 4, 6]) == [3, 4, 6]
        assert ctx.with_forced([7, 6, 5, 4, 3, 2], 5) == [7, 6, 5, 4, 3]

    def test_the_forced_card_is_in_the_best_hand_and_not_in_the_kept_one(self):
        ctx = _ctx(PAIR_HAND, forced=6)
        assert ctx.forced_card_idx == 6 and ctx.required_cards == {6}
        assert 6 in ctx.best.card_indices
        assert ctx.best_without_forced.hand_name == "Pair"
        assert sorted(ctx.kept_indices) == [0, 1]  # the Kings, by their places in the full hand

    def test_kept_hand_gives_up_a_forced_card_it_would_have_used(self):
        ctx = _ctx(PAIR_HAND, forced=0)
        assert ctx.best.hand_name == "Pair" and 0 in ctx.best.card_indices
        assert 0 not in ctx.kept_indices
        assert ctx.kept_best.hand_name == "High Card"
        assert ctx.kept_best.total < ctx.best.total

    def test_with_forced_puts_the_card_first_and_keeps_to_the_limit(self):
        ctx = _ctx(PAIR_HAND, forced=6)
        assert ctx.with_forced([3, 4, 5]) == [6, 3, 4, 5]
        assert ctx.with_forced([3, 6, 5]) == [6, 3, 5]
        assert ctx.with_forced([7, 5, 4, 3, 2]) == [6, 7, 5, 4, 3]
        assert ctx.with_forced([7], 1) == [6]


class TestDiscardCandidates:
    def test_every_candidate_discards_the_forced_card_and_keeps_the_rest_of_its_plan(self):
        hand = [card("K", "H"), card("9", "H"), card("7", "H"), card("5", "H"),
                card("4", "D"), card("3", "C"), card("K", "S"), card("2", "S")]
        deck = [card(r, "H") for r in "23468TJQA"]
        forced = 4
        found = discard_candidates(hand, HAND_LEVELS, max_discard=5, deck_cards=deck,
                                   chips_remaining=10_000, forced_index=forced)
        assert found
        for c in found:
            assert c.discard_indices[0] == forced
            assert forced not in c.keep_indices
            assert len(c.discard_indices) <= 5 and len(set(c.discard_indices)) == len(c.discard_indices)
            assert not set(c.discard_indices) & set(c.keep_indices)
            assert all(0 <= i < len(hand) for i in c.discard_indices + c.keep_indices)

    def test_matches_the_candidates_of_the_hand_without_that_card(self):
        hand = [card("K", "H"), card("9", "H"), card("7", "H"), card("5", "H"),
                card("4", "D"), card("3", "C"), card("K", "S"), card("2", "S")]
        deck = [card(r, "H") for r in "23468TJQA"]
        rest = hand[:4] + hand[5:]
        plain = discard_candidates(rest, HAND_LEVELS, max_discard=4, deck_cards=deck,
                                   chips_remaining=10_000)
        forced = discard_candidates(hand, HAND_LEVELS, max_discard=5, deck_cards=deck,
                                    chips_remaining=10_000, forced_index=4)
        shift = lambda i: i if i < 4 else i + 1  # noqa: E731
        assert [(c.chase_hand, [shift(i) for i in c.keep_indices]) for c in plain] == [
            (c.chase_hand, c.keep_indices) for c in forced]
        assert [[4] + [shift(i) for i in c.discard_indices] for c in plain] == [
            c.discard_indices for c in forced]


class TestMilkPlays:
    def test_a_milk_play_carries_the_forced_card_and_spares_the_kept_hand(self):
        ctx = _ctx(PAIR_HAND, forced=6, jokers=[joker("j_green_joker")], score=20, hands_left=4)
        action = choose_milk_play(ctx)
        assert isinstance(action, PlayCards) and action.reason.startswith("milk")
        cards = _cards(action)
        assert 6 in cards and len(cards) == 5
        assert not set(cards) & ctx.kept_indices  # the Kings stay in hand

    def test_milking_is_judged_on_the_hand_left_after_the_forced_card_goes(self):
        # The pair needs the forced King: without it the hand cannot clear the blind,
        # so there is nothing safe to milk with.
        ctx = _ctx(PAIR_HAND, forced=0, jokers=[joker("j_green_joker")], score=60, hands_left=4)
        assert ctx.best.total >= ctx.chips_remaining > ctx.kept_best.total
        assert choose_milk_play(ctx) is None

    def test_square_joker_still_gets_exactly_four_cards(self):
        ctx = _ctx(PAIR_HAND, forced=6, jokers=[joker("j_square")], score=20, hands_left=4)
        action = choose_milk_play(ctx)
        assert isinstance(action, PlayCards)
        assert len(_cards(action)) == 4 and 6 in _cards(action)

    def test_a_junk_play_carries_the_forced_card(self):
        ctx = _ctx(PAIR_HAND, forced=6, score=20)
        action = _play_junk(ctx)
        cards = _cards(action)
        assert 6 in cards and len(cards) == 5 and not set(cards) & ctx.kept_indices

    def test_the_same_hand_without_the_boss_is_unchanged(self):
        ctx = _ctx(PAIR_HAND, jokers=[joker("j_green_joker")], score=20, hands_left=4, bell=False)
        action = choose_milk_play(ctx)
        assert sorted(_cards(action)) == [3, 4, 5, 6, 7]  # the five lowest cards outside the pair


class TestSetupAndSoloPlays:
    def test_card_sharp_setup_plays_the_type_with_the_forced_card_in_it(self):
        # A Straight is there without the forced King (9 to 5), but the King comes along
        # with any play and breaks it; the Pair the King makes is what can be set up.
        hand = [card("K", "H"), card("K", "D"), card("9", "C"), card("8", "S"),
                card("7", "H"), card("6", "D"), card("5", "C"), card("2", "S")]
        ctx = _ctx(hand, forced=0, jokers=[joker("j_card_sharp")], discards_left=0)
        assert _execute_setup(ctx, "Straight") is None
        action = _execute_setup(ctx, "Pair")
        assert isinstance(action, PlayCards) and action.hand_name == "Pair"
        cards = _cards(action)
        assert 0 in cards and len(cards) <= 5
        assert classify_hand([ctx.hand_cards[i] for i in cards]) == "Pair"

    def test_trading_card_discards_the_forced_card_alone(self):
        ctx = _ctx(PAIR_HAND, forced=6, jokers=[joker("j_trading")], score=20)
        action = _trading_first_discard(ctx)
        assert isinstance(action, DiscardCards) and _cards(action) == [6]

    def test_sixth_sense_only_plays_a_forced_six(self):
        hand = [card("K", "H"), card("K", "D"), card("9", "C"), card("6", "S"),
                card("5", "H"), card("4", "D"), card("3", "C"), card("2", "S")]
        jokers = [joker("j_sixth_sense")]
        assert _sixth_sense_solo(_ctx(hand, forced=6, jokers=jokers, score=20)) is None
        action = _sixth_sense_solo(_ctx(hand, forced=3, jokers=jokers, score=20))
        assert _cards(action) == [3]

    def test_dna_only_duplicates_the_forced_card_when_it_is_the_one_worth_copying(self):
        jokers = [joker("j_dna")]
        assert _dna_solo(_ctx(PAIR_HAND, forced=6, jokers=jokers, score=20)) is None
        hand = [card("9", "H"), card("9", "D"), card("A", "C")] + PAIR_HAND[3:]
        action = _dna_solo(_ctx(hand, forced=2, jokers=jokers, score=20))
        assert _cards(action) == [2]


class TestDiscards:
    def test_a_discard_to_improve_contains_the_forced_card(self):
        hand = [card("K", "H"), card("9", "H"), card("7", "H"), card("5", "H"),
                card("4", "D"), card("3", "C"), card("J", "S"), card("2", "S")]
        deck = [card(r, "H") for r in "23468TJQA"] * 2
        ctx = _ctx(hand, forced=6, score=100_000, deck=deck)
        action = choose_discard(ctx)
        assert isinstance(action, DiscardCards)
        assert _respects_bell(action, ctx)

    def test_the_bot_does_not_plan_to_keep_the_forced_card(self):
        # Four hearts and a forced fifth heart: the flush draw the bot can keep is the
        # other four; the forced card goes out with the discard.
        hand = [card("K", "H"), card("9", "H"), card("7", "H"), card("5", "H"),
                card("4", "H"), card("3", "C"), card("J", "S"), card("2", "S")]
        deck = [card(r, "H") for r in "2368TJQA"] * 2
        ctx = _ctx(hand, forced=4, score=100_000, deck=deck)
        for c in discard_candidates(ctx.hand_cards, ctx.hand_levels, deck_cards=ctx.deck_cards,
                                    chips_remaining=ctx.chips_remaining,
                                    forced_index=ctx.forced_card_idx):
            assert 4 in c.discard_indices and 4 not in c.keep_indices


class TestEveryChoiceRespectsTheBell:
    JOKERS = ["j_green_joker", "j_square", "j_wee", "j_runner", "j_trousers", "j_supernova",
              "j_trading", "j_burnt", "j_sixth_sense", "j_dna", "j_card_sharp",
              "j_mystic_summit", "j_castle", "j_hit_the_road", "j_8_ball", "j_midas_mask",
              "j_acrobat", "j_ride_the_bus", "j_space", "j_banner"]

    def test_random_rounds(self):
        rng = random.Random(186)
        deck = [card(r, s) for r in RANKS for s in SUITS]
        checked = 0
        for _ in range(250):
            rng.shuffle(deck)
            size = rng.choice([7, 8, 8, 8, 9, 10])
            hand, rest = deck[:size], deck[size:]
            jokers = [joker(k) for k in rng.sample(self.JOKERS, rng.randint(0, 4))]
            ctx = _ctx(
                hand, forced=rng.randrange(size), jokers=jokers,
                score=rng.choice([10, 150, 800, 20_000, 100_000]),
                hands_left=rng.randint(1, 5), discards_left=rng.randint(0, 4), deck=rest,
            )
            plan = build_round_plan(ctx)
            choices = [
                execute_plan_step(plan, ctx) if plan else None,
                choose_winning_play(ctx), choose_discard(ctx), choose_high_value_play(ctx),
                choose_milk_play(ctx), choose_best_available(ctx),
            ]
            for action in choices:
                assert _respects_bell(action, ctx), (action, ctx.forced_card_idx)
                checked += isinstance(action, (PlayCards, DiscardCards))
        assert checked > 500
