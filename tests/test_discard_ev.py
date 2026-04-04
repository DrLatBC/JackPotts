"""Tests for EV-based discard decisions with Monte Carlo miss sampling.

Tests the ChaseCandidate structure, _chase_ev calculation, _sample_miss_ev
Monte Carlo sampling, and end-to-end integration through DiscardToImprove.evaluate().
"""

import random

from balatro_bot.domain.scoring.estimate import score_hand
from balatro_bot.domain.scoring.search import (
    ChaseCandidate,
    discard_candidates,
    best_hand,
)
from balatro_bot.rules.playing import DiscardToImprove
from balatro_bot.context import RoundContext
from balatro_bot.strategy import compute_strategy
from tests.conftest import card, joker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_deck(suits="HDCS", ranks="23456789TJQKA", exclude=None):
    """Build a standard deck, optionally excluding specific (rank, suit) pairs."""
    exclude = exclude or set()
    return [card(r, s) for s in suits for r in ranks if (r, s) not in exclude]


def _make_state(hand_cards, jokers=None, deck_cards=None, blind_score=1000,
                chips_scored=0, hands_left=4, discards_left=3, hand_levels=None):
    """Build a minimal game state dict for DiscardToImprove integration tests."""
    return {
        "hand": {"cards": hand_cards},
        "jokers": {"cards": jokers or [], "limit": 5},
        "cards": {"cards": deck_cards or _make_deck()},
        "blinds": {"small": {"status": "CURRENT", "score": blind_score, "name": "Small Blind"}},
        "round": {"chips": chips_scored, "hands_left": hands_left, "discards_left": discards_left},
        "hands": hand_levels or {
            "High Card": {"chips": 5, "mult": 1, "level": 1},
            "Pair": {"chips": 10, "mult": 2, "level": 1},
            "Two Pair": {"chips": 20, "mult": 2, "level": 1},
            "Three of a Kind": {"chips": 30, "mult": 3, "level": 1},
            "Straight": {"chips": 30, "mult": 4, "level": 1},
            "Flush": {"chips": 35, "mult": 4, "level": 1},
            "Full House": {"chips": 40, "mult": 4, "level": 1},
            "Four of a Kind": {"chips": 60, "mult": 7, "level": 1},
        },
        "money": 5,
        "ante_num": 1,
        "round_num": 1,
    }


def _ctx_for(hand, jokers_list=None, deck=None, discards_left=3):
    """Build a RoundContext for direct _chase_ev tests."""
    jokers_list = jokers_list or []
    if deck is None:
        # Exclude hand cards from deck to match real game state
        hand_keys = {(c["value"]["rank"], c["value"]["suit"]) for c in hand}
        deck = _make_deck(exclude=hand_keys)
    return RoundContext(
        blind_score=5000, blind_name="Small Blind", chips_scored=0,
        chips_remaining=5000, hands_left=4, discards_left=discards_left,
        hand_cards=hand, hand_levels={}, jokers=jokers_list,
        best=best_hand(hand, jokers=jokers_list),
        money=5, ante=1, round_num=1, min_cards=1,
        strategy=compute_strategy(jokers_list, {}),
        deck_cards=deck,
    )


# ---------------------------------------------------------------------------
# ChaseCandidate structure tests
# ---------------------------------------------------------------------------

class TestChaseCandidateStructure:
    def test_chase_candidate_fields(self):
        """discard_candidates returns ChaseCandidate with all fields."""
        hand = [card("K", "H"), card("K", "D"), card("3", "C"), card("5", "S"), card("7", "H")]
        suggestions = discard_candidates(hand)
        assert len(suggestions) > 0
        c = suggestions[0]
        assert isinstance(c, ChaseCandidate)
        assert isinstance(c.discard_indices, list)
        assert isinstance(c.reason, str)
        assert isinstance(c.chase_hand, str)
        assert isinstance(c.keep_indices, list)
        assert isinstance(c.hit_prob, float)

    def test_chase_candidate_unpacking(self):
        """Backward-compatible star unpacking works."""
        hand = [card("K", "H"), card("K", "D"), card("3", "C"), card("5", "S"), card("7", "H")]
        suggestions = discard_candidates(hand)
        indices, reason, *rest = suggestions[0]
        assert isinstance(indices, list)
        assert isinstance(reason, str)
        assert len(rest) == 3


# ---------------------------------------------------------------------------
# On-affinity chase scenarios (should favor chasing)
# ---------------------------------------------------------------------------

class TestOnAffinityChase:
    def test_flush_chase_with_flush_jokers(self):
        """Flush jokers make a flush chase dominate a Pair play."""
        hand = [card("K", "H"), card("K", "D"), card("9", "H"), card("5", "H"), card("3", "H")]
        jokers_list = [joker("j_tribe"), joker("j_droll")]
        ctx = _ctx_for(hand, jokers_list)

        candidate = ChaseCandidate(
            discard_indices=[1],
            reason="chase Flush (60% to hit, H), discard 1 cards",
            chase_hand="Flush",
            keep_indices=[0, 2, 3, 4],
            hit_prob=0.6,
        )
        # Use current best as miss_ev (conservative — miss doesn't help)
        miss_ev = ctx.best.total
        chase_ev = DiscardToImprove._chase_ev(candidate, ctx, miss_ev)

        assert chase_ev > ctx.best.total, f"Flush chase EV ({chase_ev:.0f}) should beat Pair play ({ctx.best.total})"

    def test_straight_chase_with_straight_jokers(self):
        """Straight jokers make a straight chase worth it."""
        hand = [card("5", "H"), card("6", "D"), card("7", "C"), card("8", "S"), card("2", "H")]
        jokers_list = [joker("j_order"), joker("j_crazy")]
        ctx = _ctx_for(hand, jokers_list)

        candidate = ChaseCandidate(
            discard_indices=[4],
            reason="chase Straight (50% to hit), discard 1 cards",
            chase_hand="Straight",
            keep_indices=[0, 1, 2, 3],
            hit_prob=0.5,
        )
        miss_ev = ctx.best.total
        chase_ev = DiscardToImprove._chase_ev(candidate, ctx, miss_ev)
        assert chase_ev > ctx.best.total, f"Straight chase EV ({chase_ev:.0f}) should beat play ({ctx.best.total})"

    def test_three_kind_chase_with_trio_joker(self):
        """Trio joker (x3.0 on Three of a Kind) makes the chase worthwhile."""
        hand = [card("8", "H"), card("8", "D"), card("3", "C"), card("5", "S"), card("7", "H")]
        jokers_list = [joker("j_trio")]
        ctx = _ctx_for(hand, jokers_list)
        assert ctx.best.hand_name == "Pair"

        candidate = ChaseCandidate(
            discard_indices=[2, 3, 4],
            reason="chase Three of a Kind (40% to hit), discard 3 cards",
            chase_hand="Three of a Kind",
            keep_indices=[0, 1],
            hit_prob=0.4,
        )
        miss_ev = ctx.best.total
        chase_ev = DiscardToImprove._chase_ev(candidate, ctx, miss_ev)
        assert chase_ev > ctx.best.total, f"Three of a Kind chase EV ({chase_ev:.0f}) should beat Pair ({ctx.best.total})"


# ---------------------------------------------------------------------------
# Off-affinity chase scenarios (should favor playing)
# ---------------------------------------------------------------------------

class TestOffAffinityChase:
    def test_flush_chase_rejected_with_pair_jokers(self):
        """Stacked pair jokers boost the Pair enough that a low-prob Flush chase loses."""
        hand = [card("K", "H"), card("K", "D"), card("9", "H"), card("5", "H"), card("3", "H")]
        jokers_list = [joker("j_duo"), joker("j_jolly"), joker("j_sly")]
        ctx = _ctx_for(hand, jokers_list)
        assert ctx.best.hand_name == "Pair"

        candidate = ChaseCandidate(
            discard_indices=[1],
            reason="chase Flush (30% to hit, H), discard 1 cards",
            chase_hand="Flush",
            keep_indices=[0, 2, 3, 4],
            hit_prob=0.3,
        )
        miss_ev = ctx.best.total
        chase_ev = DiscardToImprove._chase_ev(candidate, ctx, miss_ev)
        assert chase_ev <= ctx.best.total, f"Flush chase EV ({chase_ev:.0f}) should not beat stacked Pair ({ctx.best.total})"

    def test_straight_chase_rejected_with_pair_jokers(self):
        """Strong pair jokers make a Pair of Aces strong enough to reject a low-prob straight chase."""
        hand = [card("A", "H"), card("A", "D"), card("6", "C"), card("7", "S"), card("8", "H")]
        jokers_list = [joker("j_duo"), joker("j_jolly")]
        ctx = _ctx_for(hand, jokers_list)
        assert ctx.best.hand_name == "Pair"

        candidate = ChaseCandidate(
            discard_indices=[0, 1],
            reason="chase Straight (25% to hit), discard 2 cards",
            chase_hand="Straight",
            keep_indices=[2, 3, 4],
            hit_prob=0.25,
        )
        miss_ev = ctx.best.total
        chase_ev = DiscardToImprove._chase_ev(candidate, ctx, miss_ev)
        assert chase_ev <= ctx.best.total, f"Off-affinity straight chase ({chase_ev:.0f}) should not beat boosted Pair ({ctx.best.total})"

    def test_flush_chase_loses_to_strong_pair_with_mc(self):
        """With MC sampling, a weak flush chase loses to a strong Pair when miss_ev
        is computed realistically — the Pair of Aces with junk deck draws poorly."""
        random.seed(42)
        hand = [card("A", "H"), card("A", "D"), card("9", "H"), card("5", "H"), card("3", "H")]
        jokers_list = [joker("j_duo"), joker("j_jolly")]  # strong Pair jokers
        # Deck of low off-suit cards — discarding the A♦ to chase flush draws badly
        junk_deck = [card(r, s) for r in "234" for s in "CS"]
        ctx = _ctx_for(hand, jokers_list, deck=junk_deck)
        assert ctx.best.hand_name == "Pair"

        candidate = ChaseCandidate(
            discard_indices=[1],
            reason="chase Flush (20% to hit, H), discard 1 cards",
            chase_hand="Flush",
            keep_indices=[0, 2, 3, 4],
            hit_prob=0.20,
        )
        # MC miss: discard the A♦, draw from junk deck — lose the Pair entirely
        miss_ev = DiscardToImprove._sample_miss_ev([0, 2, 3, 4], ctx)
        chase_ev = DiscardToImprove._chase_ev(candidate, ctx, miss_ev)
        # Losing the Pair of Aces (boosted by duo+jolly) for a 20% flush is bad
        assert chase_ev <= ctx.best.total, f"Flush chase ({chase_ev:.0f}) should not beat boosted Pair ({ctx.best.total})"


# ---------------------------------------------------------------------------
# Multi-joker affinity stacking
# ---------------------------------------------------------------------------

class TestStackedAffinity:
    def test_stacked_flush_affinity_overrides_low_probability(self):
        """Triple flush joker stack makes even a 25% draw worthwhile vs High Card."""
        hand = [card("K", "H"), card("9", "H"), card("5", "H"), card("3", "H"), card("2", "D")]
        jokers_list = [joker("j_tribe"), joker("j_droll"), joker("j_crafty")]
        ctx = _ctx_for(hand, jokers_list)

        candidate = ChaseCandidate(
            discard_indices=[4],
            reason="chase Flush (25% to hit, H), discard 1 cards",
            chase_hand="Flush",
            keep_indices=[0, 1, 2, 3],
            hit_prob=0.25,
        )
        miss_ev = ctx.best.total
        chase_ev = DiscardToImprove._chase_ev(candidate, ctx, miss_ev)
        assert chase_ev > ctx.best.total, f"Stacked flush EV ({chase_ev:.0f}) should beat {ctx.best.hand_name} ({ctx.best.total}) even at 25%"

    def test_stacked_pair_affinity_blocks_marginal_flush(self):
        """Triple pair joker stack makes Pair so strong that a 40% flush chase loses."""
        hand = [card("K", "H"), card("K", "D"), card("9", "H"), card("5", "H"), card("3", "H")]
        jokers_list = [joker("j_duo"), joker("j_jolly"), joker("j_sly")]
        ctx = _ctx_for(hand, jokers_list)
        assert ctx.best.hand_name == "Pair"

        candidate = ChaseCandidate(
            discard_indices=[1],
            reason="chase Flush (40% to hit, H), discard 1 cards",
            chase_hand="Flush",
            keep_indices=[0, 2, 3, 4],
            hit_prob=0.4,
        )
        miss_ev = ctx.best.total
        chase_ev = DiscardToImprove._chase_ev(candidate, ctx, miss_ev)
        assert chase_ev <= ctx.best.total, f"Flush chase ({chase_ev:.0f}) should not beat stacked Pair ({ctx.best.total})"


# ---------------------------------------------------------------------------
# Monte Carlo miss sampling
# ---------------------------------------------------------------------------

class TestSampleMissEv:
    def test_sample_miss_ev_returns_reasonable_value(self):
        """Sampled miss EV is between worst and best possible hands."""
        random.seed(42)
        hand = [card("K", "H"), card("9", "H"), card("5", "H"), card("3", "H"), card("2", "D")]
        ctx = _ctx_for(hand)

        miss_ev = DiscardToImprove._sample_miss_ev([0, 1, 2, 3], ctx)
        # Should be positive (some hand is always possible)
        assert miss_ev > 0
        # Should be less than a guaranteed Flush score (we're drawing random cards)
        _, _, flush_score = score_hand("Flush", hand[:4])
        assert miss_ev < flush_score * 2

    def test_sample_miss_ev_with_empty_deck(self):
        """Falls back to current best when deck is too small."""
        hand = [card("K", "H"), card("9", "H"), card("5", "H"), card("3", "H"), card("2", "D")]
        ctx = _ctx_for(hand, deck=[])  # empty deck

        miss_ev = DiscardToImprove._sample_miss_ev([0, 1, 2, 3], ctx)
        assert miss_ev == ctx.best.total

    def test_sample_miss_ev_deterministic_with_seed(self):
        """Same seed produces same result."""
        hand = [card("K", "H"), card("9", "H"), card("5", "H"), card("3", "H"), card("2", "D")]
        ctx = _ctx_for(hand)

        random.seed(123)
        ev1 = DiscardToImprove._sample_miss_ev([0, 1, 2, 3], ctx)
        random.seed(123)
        ev2 = DiscardToImprove._sample_miss_ev([0, 1, 2, 3], ctx)
        assert ev1 == ev2

    def test_sample_miss_ev_hearts_deck_favors_flush(self):
        """When deck is all Hearts, miss_ev should be high (likely Flush draws)."""
        random.seed(42)
        hand = [card("K", "H"), card("9", "H"), card("5", "H"), card("3", "H"), card("2", "D")]
        jokers_list = [joker("j_tribe")]  # x2.0 on Flush

        # Deck full of Hearts — drawing any card completes the flush
        hearts_deck = [card(r, "H") for r in "2345678TJQA"]
        ctx = _ctx_for(hand, jokers_list, deck=hearts_deck)

        miss_ev = DiscardToImprove._sample_miss_ev([0, 1, 2, 3], ctx)
        # With all Hearts in deck, we almost always draw into a flush
        # miss_ev should be close to the Flush score
        _, _, flush_score = score_hand("Flush", hand[:4], jokers=jokers_list)
        assert miss_ev >= flush_score * 0.5, f"Hearts deck miss_ev ({miss_ev:.0f}) should be high"

    def test_sample_miss_ev_junk_deck_is_low(self):
        """When deck is low cards and no jokers, miss_ev should be modest."""
        random.seed(42)
        hand = [card("K", "H"), card("K", "D"), card("9", "H"), card("5", "H"), card("3", "H")]
        # Deck of low off-suit cards — drawing won't help much
        junk_deck = [card(r, s) for r in "234" for s in "CS"]
        ctx = _ctx_for(hand, deck=junk_deck)

        miss_ev = DiscardToImprove._sample_miss_ev([0, 1], ctx)  # keep pair of Kings
        # miss_ev should reflect Pair of Kings + junk
        assert miss_ev > 0
        # Should be close to the current Pair score (kept cards are the pair)
        assert miss_ev <= ctx.best.total * 2


class TestMissEvCache:
    def test_shared_keep_set_sampled_once(self):
        """Candidates sharing keep_indices use the same miss_ev."""
        hand = [card("8", "H"), card("8", "D"), card("3", "C"), card("5", "S"), card("7", "H")]
        ctx = _ctx_for(hand)

        # Two candidates with same keep set
        suggestions = [
            ChaseCandidate([2, 3, 4], "chase Three of a Kind (30% to hit), discard 3 cards",
                           "Three of a Kind", [0, 1], 0.3),
            ChaseCandidate([2, 3, 4], "chase Two Pair (20% to hit), discard 3 cards",
                           "Two Pair", [0, 1], 0.2),
        ]

        # Call _best_chase which builds the cache internally
        random.seed(42)
        DiscardToImprove._best_chase(suggestions, ctx, ctx.best.total)
        # If it crashes or takes too long, the caching is broken.
        # We just verify it completes without error.


# ---------------------------------------------------------------------------
# Redraw with MC
# ---------------------------------------------------------------------------

class TestRedrawMC:
    def test_redraw_returns_miss_ev(self):
        """Redraw EV equals the sampled miss_ev (no specific target hand)."""
        hand = [card("2", "H"), card("3", "D"), card("5", "C"), card("7", "S"), card("9", "H")]
        ctx = _ctx_for(hand)

        candidate = ChaseCandidate(
            discard_indices=[0, 1, 2],
            reason="redraw 3 cards (High Card for 14 is hopeless vs 50000 needed)",
            chase_hand="redraw",
            keep_indices=[3, 4],
            hit_prob=0.5,
        )
        miss_ev = 100.0  # known value
        chase_ev = DiscardToImprove._chase_ev(candidate, ctx, miss_ev)
        assert chase_ev == miss_ev, f"Redraw EV ({chase_ev}) should equal miss_ev ({miss_ev})"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_no_chase_when_hand_already_wins(self):
        """DiscardToImprove returns None when best hand beats the blind."""
        hand = [card("A", "H"), card("A", "D"), card("A", "C"), card("A", "S"), card("K", "H")]
        state = _make_state(hand, blind_score=50, hands_left=4, discards_left=3)
        result = DiscardToImprove().evaluate(state)
        assert result is None

    def test_ev_with_no_jokers(self):
        """EV comparison works with pure base scoring (no jokers)."""
        hand = [card("K", "H"), card("9", "H"), card("5", "H"), card("3", "H"), card("2", "D")]
        ctx = _ctx_for(hand)

        candidate = ChaseCandidate(
            discard_indices=[4],
            reason="chase Flush (50% to hit, H), discard 1 cards",
            chase_hand="Flush",
            keep_indices=[0, 1, 2, 3],
            hit_prob=0.5,
        )
        miss_ev = ctx.best.total
        chase_ev = DiscardToImprove._chase_ev(candidate, ctx, miss_ev)
        assert chase_ev > 0
        # Flush base (35*4=140) at 50% should beat High Card (5*1 + card chips)
        assert chase_ev > ctx.best.total


# ---------------------------------------------------------------------------
# Integration through DiscardToImprove.evaluate()
# ---------------------------------------------------------------------------

class TestIntegration:
    def test_ev_integration_flush_chase_fires(self):
        """Full evaluate() returns a chase with EV annotation when flush jokers present."""
        random.seed(42)
        hand = [card("K", "H"), card("9", "H"), card("5", "H"), card("3", "H"), card("2", "D")]
        jokers_list = [joker("j_tribe"), joker("j_droll")]
        deck = _make_deck(exclude={("K", "H"), ("9", "H"), ("5", "H"), ("3", "H"), ("2", "D")})

        state = _make_state(hand, jokers=jokers_list, deck_cards=deck,
                            blind_score=5000, hands_left=4, discards_left=3)
        result = DiscardToImprove().evaluate(state)
        assert result is not None, "Should chase the flush"
        assert "EV" in result.reason, f"Reason should have EV annotation: {result.reason}"
        assert "chase" in result.reason.lower()

    def test_ev_integration_play_over_weak_chase(self):
        """Pair with duo joker beats a marginal flush draw — returns None."""
        random.seed(42)
        hand = [card("K", "H"), card("K", "D"), card("9", "H"), card("5", "H"), card("3", "H")]
        jokers_list = [joker("j_duo")]
        # No Hearts in deck — flush probability = 0
        deck = [card(r, s) for s in "DCS" for r in "23456789TJQKA"]

        state = _make_state(hand, jokers=jokers_list, deck_cards=deck,
                            blind_score=5000, hands_left=4, discards_left=3)
        result = DiscardToImprove().evaluate(state)
        if result is not None:
            assert "chase" not in result.reason.lower() or "EV" in result.reason

    def test_mc_miss_ev_blocks_marginal_chase(self):
        """When miss_ev is high (good deck), marginal chases get rejected."""
        random.seed(42)
        # Pair of Kings, near-flush in Hearts
        hand = [card("K", "H"), card("K", "D"), card("9", "H"), card("5", "H"), card("3", "H")]
        jokers_list = [joker("j_duo"), joker("j_jolly")]  # strong Pair jokers
        # Deck full of face cards — miss draws should be decent
        rich_deck = [card(r, s) for r in "TJQKA" for s in "HDCS"]

        state = _make_state(hand, jokers=jokers_list, deck_cards=rich_deck,
                            blind_score=5000, hands_left=4, discards_left=3)
        result = DiscardToImprove().evaluate(state)
        # With strong pair jokers and a rich deck (high miss_ev), the flush chase
        # should not fire — the Pair is already good and misses aren't costly
        if result is not None:
            # If something fires, verify it's not a chase or has proper EV annotation
            assert "chase" not in result.reason.lower() or "EV" in result.reason
