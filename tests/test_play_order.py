"""Tests for _sort_play_order and _pad_with_junk — card ordering for scoring."""

from __future__ import annotations

from balatro_bot.rules._helpers import _sort_play_order, _pad_with_junk
from tests.conftest import card, joker


def test_hanging_chad_even_steven_prefers_even_card_first():
    """Even card should go first to get 3x Even Steven +4 mult triggers."""
    hand = [
        card("9", "S"),  # 0: odd — no Even Steven bonus
        card("J", "C"),  # 1: odd
        card("9", "D"),  # 2: odd
        card("8", "C"),  # 3: even — Even Steven +4 mult
        card("4", "C"),  # 4: even — Even Steven +4 mult
    ]
    jokers = [joker("j_hanging_chad"), joker("j_even_steven")]
    result = _sort_play_order([0, 1, 2, 3, 4], hand, jokers)
    # First card must be even (index 3 or 4), not odd
    assert result[0] in (3, 4), f"Expected even card first, got index {result[0]}"


def test_hanging_chad_odd_todd_prefers_odd_card_first():
    """Odd card should go first to get 3x Odd Todd +31 chips triggers."""
    hand = [
        card("T", "H"),  # 0: even
        card("8", "D"),  # 1: even
        card("7", "S"),  # 2: odd — Odd Todd +31 chips
        card("6", "C"),  # 3: even
        card("4", "H"),  # 4: even
    ]
    jokers = [joker("j_hanging_chad"), joker("j_odd_todd")]
    result = _sort_play_order([0, 1, 2, 3, 4], hand, jokers)
    assert result[0] == 2, f"Expected odd card (7♠) first, got index {result[0]}"


def test_hanging_chad_fibonacci_prefers_fib_card_first():
    """Fibonacci card should go first for 3x +8 mult triggers."""
    hand = [
        card("T", "H"),  # 0: not fibonacci
        card("9", "D"),  # 1: not fibonacci
        card("A", "S"),  # 2: fibonacci — +8 mult
        card("6", "C"),  # 3: not fibonacci
        card("4", "H"),  # 4: not fibonacci
    ]
    jokers = [joker("j_hanging_chad"), joker("j_fibonacci")]
    result = _sort_play_order([0, 1, 2, 3, 4], hand, jokers)
    assert result[0] == 2, f"Expected Ace (fibonacci) first, got index {result[0]}"


def test_hanging_chad_smiley_face_prefers_face_first():
    """Face card should go first for 3x Smiley Face +5 mult triggers."""
    hand = [
        card("9", "H"),  # 0: not face
        card("8", "D"),  # 1: not face
        card("7", "S"),  # 2: not face
        card("Q", "C"),  # 3: face — Smiley +5 mult
        card("4", "H"),  # 4: not face
    ]
    jokers = [joker("j_hanging_chad"), joker("j_smiley")]
    result = _sort_play_order([0, 1, 2, 3, 4], hand, jokers)
    assert result[0] == 3, f"Expected Queen (face) first, got index {result[0]}"


def test_hanging_chad_no_per_card_jokers_still_works():
    """Without per-card jokers, ordering should still be deterministic."""
    hand = [
        card("A", "S"),  # 0
        card("K", "H"),  # 1
        card("Q", "D"),  # 2
    ]
    jokers = [joker("j_hanging_chad")]
    result = _sort_play_order([0, 1, 2], hand, jokers)
    # All have xm=1.0, so score = 1.0 + 2*add; Ace has highest chip value
    assert result[0] == 0, f"Expected Ace first (highest chips), got index {result[0]}"


def test_hanging_chad_glass_xmult_beats_additive():
    """Glass card (x2 mult) with xm^3=8.0 should beat additive bonuses."""
    hand = [
        card("8", "H"),  # 0: even — Even Steven +4 mult, no xmult
        card("T", "D", enhancement="GLASS"),  # 1: even + Glass x2
        card("6", "S"),  # 2: even
    ]
    jokers = [joker("j_hanging_chad"), joker("j_even_steven")]
    result = _sort_play_order([0, 1, 2], hand, jokers)
    # Glass x2: xm^3 = 8.0, plus 2*add. Should beat plain even card's 1.0 + 2*add
    assert result[0] == 1, f"Expected Glass card first, got index {result[0]}"


def test_hanging_chad_debuffed_card_never_first():
    """Debuffed card should never be placed first."""
    debuffed = card("A", "S")
    debuffed["state"]["debuff"] = True
    hand = [
        debuffed,         # 0: debuffed
        card("2", "H"),   # 1: low but not debuffed
        card("3", "D"),   # 2: low but not debuffed
    ]
    jokers = [joker("j_hanging_chad"), joker("j_even_steven")]
    result = _sort_play_order([0, 1, 2], hand, jokers)
    assert result[0] != 0, f"Debuffed card should not be first, got index {result[0]}"


def test_hanging_chad_arrowhead_prefers_spade_first():
    """Arrowhead (+50 chips per spade) should heavily favor spade in first slot."""
    hand = [
        card("T", "H"),  # 0: heart
        card("T", "D"),  # 1: diamond
        card("T", "S"),  # 2: spade — Arrowhead +50 chips
    ]
    jokers = [joker("j_hanging_chad"), joker("j_arrowhead")]
    result = _sort_play_order([0, 1, 2], hand, jokers)
    assert result[0] == 2, f"Expected spade card first (Arrowhead), got index {result[0]}"


def test_hanging_chad_multiple_joker_bonuses_stack():
    """Card matching multiple per-card jokers should be preferred."""
    hand = [
        card("8", "S"),  # 0: even (Even Steven +4) + spade (Wrathful +3)
        card("9", "H"),  # 1: odd, heart — no bonuses from these jokers
        card("7", "D"),  # 2: odd, diamond — no bonuses
    ]
    jokers = [joker("j_hanging_chad"), joker("j_even_steven"), joker("j_wrathful_joker")]
    result = _sort_play_order([0, 1, 2], hand, jokers)
    assert result[0] == 0, f"Expected 8♠ first (even+spade bonuses), got index {result[0]}"


def test_no_hanging_chad_glass_goes_last():
    """Without Hanging Chad, Glass (xmult) cards should go rightmost."""
    hand = [
        card("A", "S", enhancement="GLASS"),  # 0: glass x2
        card("K", "H"),                        # 1: no xmult
        card("Q", "D"),                        # 2: no xmult
    ]
    jokers = []  # no hanging chad
    result = _sort_play_order([0, 1, 2], hand, jokers)
    assert result[-1] == 0, f"Expected Glass card last, got index {result[-1]} last"


def test_rearrange_needed_when_order_differs():
    """Verify that _sort_play_order can produce non-ascending indices,
    which signals the bot to call rearrange before playing."""
    hand = [
        card("3", "H"),  # 0: low value
        card("2", "D"),  # 1: low value
        card("8", "S"),  # 2: Even Steven + Wrathful — highest per-trigger
    ]
    jokers = [joker("j_hanging_chad"), joker("j_even_steven"), joker("j_wrathful_joker")]
    result = _sort_play_order([0, 1, 2], hand, jokers)
    assert result[0] == 2, "8♠ should be first"
    assert result != sorted(result), "Order should differ from ascending — triggers rearrange"


# ---------------------------------------------------------------------------
# _pad_with_junk — High Card junk rank filtering
# ---------------------------------------------------------------------------

def test_pad_high_card_no_higher_rank_junk():
    """Junk cards must not outrank the High Card scoring card."""
    hand = [
        card("K", "H"),  # 0: junk candidate — outranks scoring card
        card("Q", "D"),  # 1: junk candidate — outranks scoring card
        card("3", "S", enhancement="GLASS"),  # 2: scoring card (picked for xmult)
        card("2", "C"),  # 3: safe junk
        card("4", "H"),  # 4: safe junk
    ]
    padded = _pad_with_junk([2], hand, [], intended_hand="High Card", max_cards=5)
    # K and Q should be excluded — they'd become the scoring card
    assert 0 not in padded, "King should not be added as junk (outranks 3)"
    assert 1 not in padded, "Queen should not be added as junk (outranks 3)"
    assert 2 in padded, "Scoring card must be in padded"
    assert 3 in padded, "2♣ is safe junk"
    assert 4 not in padded or 4 in padded, "4♥ may or may not be added"


def test_pad_high_card_allows_lower_rank():
    """Junk cards with lower rank than scoring card are fine."""
    hand = [
        card("A", "S"),  # 0: scoring card (Ace = highest)
        card("2", "H"),  # 1: safe junk
        card("4", "D"),  # 2: safe junk (skip 3 to avoid straight)
        card("7", "C"),  # 3: safe junk
        card("9", "H"),  # 4: safe junk
    ]
    padded = _pad_with_junk([0], hand, [], intended_hand="High Card", max_cards=5)
    assert len(padded) == 5, "Should pad to 5 with Ace as highest"
    assert padded[0] == 0, "Ace should stay as scoring card"


def test_pad_non_high_card_allows_any_rank():
    """For non-High Card hands, junk rank filtering should not apply."""
    hand = [
        card("3", "S"),  # 0: scoring (part of pair)
        card("3", "H"),  # 1: scoring (part of pair)
        card("K", "D"),  # 2: high rank junk
        card("A", "C"),  # 3: highest rank junk
        card("2", "H"),  # 4: low rank junk
    ]
    padded = _pad_with_junk([0, 1], hand, [], intended_hand="Pair", max_cards=5)
    # For Pair, high-rank junk is fine
    assert len(padded) == 5, "Should pad to 5 for Pair"
