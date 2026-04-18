"""Tests for held-card joker awareness in hand selection.

Baron (xMult per held King), Shoot the Moon (+mult per held Queen), and
Raised Fist (+2 × lowest held rank as mult) all reward cards that are in
the hand zone but NOT played. `enumerate_hands` passes the non-played
subset as `held_cards` to `score_hand`, so the joker pipeline applies
these effects correctly and `best_hand` should naturally prefer hands
that leave K/Q held when the alternative is still viable.

These tests are behavioral guards against someone later breaking the
held_cards plumbing (e.g., passing [] or the scoring subset).
"""

from __future__ import annotations

from balatro_bot.domain.scoring.search import best_hand
from tests.conftest import card, joker


_BASIC_LEVELS = {
    "High Card": {"chips": 5, "mult": 1, "level": 1},
    "Pair": {"chips": 10, "mult": 2, "level": 1},
}


def _baron() -> dict:
    j = joker("j_baron", "Baron")
    j["value"] = {"ability": {"extra": 1.5}, "effect": "X1.5 Mult per King held in hand"}
    return j


def _shoot_the_moon() -> dict:
    j = joker("j_shoot_the_moon", "Shoot the Moon")
    j["value"] = {"ability": {"extra": 13}, "effect": "+13 Mult per Queen held in hand"}
    return j


def _raised_fist() -> dict:
    j = joker("j_raised_fist", "Raised Fist")
    j["value"] = {"effect": "Adds double the rank of lowest card held in hand to Mult"}
    return j


class TestBaronHeldKings:
    def test_prefers_triple_7s_over_playing_kings_when_baron_owned(self):
        """With Baron and a ToaK of 7s available while holding 2 Kings,
        the bot should play the 7s and leave both Kings held for Baron."""
        levels = {
            **_BASIC_LEVELS,
            "Three of a Kind": {"chips": 30, "mult": 3, "level": 1},
        }
        hand = [
            card("K", "S"),  # 0
            card("K", "D"),  # 1
            card("7", "C"),  # 2
            card("7", "H"),  # 3
            card("7", "D"),  # 4
            card("3", "S"),  # 5
            card("2", "C"),  # 6
        ]
        best = best_hand(hand, levels, jokers=[_baron()])
        played = {hand[i].value.rank for i in best.card_indices}
        # ToaK 7s with Baron boost: (30+21) × (3 × 1.5²) = 344 beats
        # Pair of Kings: (10+10+10) × 2 = 60.
        assert best.hand_name == "Three of a Kind"
        assert "K" not in played, (
            f"Baron should prefer holding Kings, but played {played} "
            f"(indices={best.card_indices}, total={best.total})"
        )
        assert played == {"7"}

    def test_plays_kings_when_no_alternative_competitive(self):
        """Control: if the only scoring option involves Kings, Baron shouldn't
        stop the bot from playing them (half a Baron boost > zero score)."""
        hand = [
            card("K", "S"),
            card("K", "D"),
            card("3", "C"),
            card("5", "H"),
            card("2", "C"),
        ]
        best = best_hand(hand, _BASIC_LEVELS, jokers=[_baron()])
        played = {hand[i].value.rank for i in best.card_indices}
        # Only Pair available is KK. High Card alternative holds both Kings but
        # scores way worse. Bot should play the Kings.
        assert best.hand_name == "Pair"
        assert played == {"K"}


class TestShootTheMoonHeldQueens:
    def test_prefers_playing_non_queens_with_stm_held_queens(self):
        """With Shoot the Moon and 2 Queens held, +13 mult × 2 queens = +26
        mult added should swing even Pair of 2s above Pair of Queens."""
        hand = [
            card("Q", "S"),  # 0
            card("Q", "D"),  # 1
            card("2", "C"),  # 2
            card("2", "H"),  # 3
            card("5", "S"),  # 4
        ]
        best = best_hand(hand, _BASIC_LEVELS, jokers=[_shoot_the_moon()])
        played = {hand[i].value.rank for i in best.card_indices}
        # Pair 2s with StM: chips (10+2+2)=14, mult (2+26)=28 → 392.
        # Pair Qs no boost: (10+10+10)×2 = 60. Pair 2s wins decisively.
        assert best.hand_name == "Pair"
        assert "Q" not in played, (
            f"Shoot the Moon should prefer holding Queens, got played={played} "
            f"(indices={best.card_indices}, total={best.total})"
        )
        assert played == {"2"}


class TestRaisedFistPrefersHighLowestHeld:
    def test_prefers_holding_ace_for_maximum_rf_mult(self):
        """Raised Fist adds 2× chips of lowest held card. Ace = 11 chips →
        +22 mult (max). Bot should hold the Ace and play the rest."""
        # Size-4 play options (High Card, scoring card = highest played):
        #   Play [A,9,7,5], held=[2]: chips (5+11)=16, mult (1+4)=5  → 80
        #   Play [9,7,5,2], held=[A]: chips (5+9)=14, mult (1+22)=23 → 322
        # Holding the Ace wins decisively.
        hand = [
            card("A", "S"),  # 0
            card("9", "D"),  # 1
            card("7", "C"),  # 2
            card("5", "H"),  # 3
            card("2", "C"),  # 4
        ]
        best = best_hand(hand, _BASIC_LEVELS, jokers=[_raised_fist()])
        played = [hand[i].value.rank for i in best.card_indices]
        assert "A" not in played, (
            f"Raised Fist should prefer holding the Ace (+22 mult) — "
            f"but played {played} (total={best.total})"
        )


class TestBaronStackingWithMoreHeldKings:
    def test_four_held_kings_heavily_biases_toward_weaker_play(self):
        """Edge case: with 4 held Kings available, Baron's X1.5⁴ = X5.06
        dominates. Even Pair of 2s should beat Pair of Kings."""
        hand = [
            card("K", "S"),  # 0
            card("K", "D"),  # 1
            card("K", "C"),  # 2
            card("K", "H"),  # 3
            card("2", "C"),  # 4
            card("2", "D"),  # 5
        ]
        # Note: Four of a Kind would be the raw winner without Baron.
        # But with 4 Kings held + Pair of 2s, the held boost can still lose
        # to 4oaK's raw chip/mult. This test specifically checks that when
        # we restrict to Pair, the bot picks 2s over Kings.
        best = best_hand(
            hand, {**_BASIC_LEVELS,
                   "Three of a Kind": {"chips": 30, "mult": 3, "level": 1},
                   "Four of a Kind": {"chips": 60, "mult": 7, "level": 1}},
            jokers=[_baron()], required_hand="Pair",
        )
        played = {hand[i].value.rank for i in best.card_indices}
        assert played == {"2"}, (
            f"With 4 held Kings, Baron should prefer Pair of 2s over Pair of Kings, "
            f"got {played} (total={best.total})"
        )
