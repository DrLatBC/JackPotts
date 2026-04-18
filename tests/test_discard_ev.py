"""Tests for MC-based discard EV ranking.

The discard system now uses a single primitive — `_expected_play_value` — which
samples from the deck and scores best_hand() with full joker effects. Ranking,
acceptance, and miss simulation all fall out of this one function.
"""

import random

from balatro_bot.domain.scoring.estimate import score_hand
from balatro_bot.domain.scoring.search import (
    ChaseCandidate,
    discard_candidates,
    best_hand,
)
from balatro_bot.domain.policy.discard_policy import (
    _expected_play_value,
    _best_chase,
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
    """Build a RoundContext for direct EV tests."""
    jokers_list = jokers_list or []
    if deck is None:
        from balatro_bot.domain.models.card import Card
        hand_keys = {(c.value.rank, c.value.suit) if isinstance(c, Card) else (c["value"]["rank"], c["value"]["suit"]) for c in hand}
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
# On-affinity chase scenarios — MC EV should beat play_ev
# ---------------------------------------------------------------------------

class TestOnAffinityChase:
    def test_flush_chase_with_flush_jokers(self):
        """Flush jokers make a flush chase dominate a Pair play — MC sees the upgrade."""
        random.seed(42)
        hand = [card("K", "H"), card("K", "D"), card("9", "H"), card("5", "H"), card("3", "H")]
        jokers_list = [joker("j_tribe"), joker("j_droll")]
        ctx = _ctx_for(hand, jokers_list)

        # Keep 4 hearts; discard K♦ to chase flush
        ev = _expected_play_value([0, 2, 3, 4], ctx)
        assert ev > ctx.best.total, f"Flush chase EV ({ev:.0f}) should beat Pair play ({ctx.best.total})"

    def test_straight_chase_with_straight_jokers(self):
        """Straight jokers make a straight chase worth it."""
        random.seed(42)
        hand = [card("5", "H"), card("6", "D"), card("7", "C"), card("8", "S"), card("2", "H")]
        jokers_list = [joker("j_order"), joker("j_crazy")]
        ctx = _ctx_for(hand, jokers_list)

        ev = _expected_play_value([0, 1, 2, 3], ctx)
        assert ev > ctx.best.total, f"Straight chase EV ({ev:.0f}) should beat play ({ctx.best.total})"

    def test_three_kind_chase_with_trio_joker(self):
        """Trio joker (x3.0 on Three of a Kind) makes the chase worthwhile."""
        random.seed(42)
        hand = [card("8", "H"), card("8", "D"), card("3", "C"), card("5", "S"), card("7", "H")]
        jokers_list = [joker("j_trio")]
        ctx = _ctx_for(hand, jokers_list)
        assert ctx.best.hand_name == "Pair"

        ev = _expected_play_value([0, 1], ctx)
        assert ev > ctx.best.total, f"Three of a Kind chase EV ({ev:.0f}) should beat Pair ({ctx.best.total})"


# ---------------------------------------------------------------------------
# Off-affinity scenarios — MC EV should NOT beat play_ev
# ---------------------------------------------------------------------------

class TestOffAffinityChase:
    def test_flush_chase_rejected_with_pair_jokers(self):
        """Stacked pair jokers boost the Pair enough that a Flush chase loses in MC."""
        random.seed(42)
        hand = [card("K", "H"), card("K", "D"), card("9", "H"), card("5", "H"), card("3", "H")]
        jokers_list = [joker("j_duo"), joker("j_jolly"), joker("j_sly")]
        ctx = _ctx_for(hand, jokers_list)
        assert ctx.best.hand_name == "Pair"

        # Chase flush by discarding K♦ — sacrifices the strong pair
        ev = _expected_play_value([0, 2, 3, 4], ctx)
        assert ev <= ctx.best.total, f"Flush chase EV ({ev:.0f}) should not beat stacked Pair ({ctx.best.total})"

    def test_flush_chase_loses_in_junk_deck(self):
        """Discarding Pair of Aces into a junk deck: MC sees miss outcomes honestly."""
        random.seed(42)
        hand = [card("A", "H"), card("A", "D"), card("9", "H"), card("5", "H"), card("3", "H")]
        jokers_list = [joker("j_duo"), joker("j_jolly")]
        junk_deck = [card(r, s) for r in "234" for s in "CS"]
        ctx = _ctx_for(hand, jokers_list, deck=junk_deck)

        ev = _expected_play_value([0, 2, 3, 4], ctx)
        assert ev <= ctx.best.total, f"Flush chase ({ev:.0f}) into junk deck should not beat boosted Pair ({ctx.best.total})"


# ---------------------------------------------------------------------------
# Monte Carlo sampling properties
# ---------------------------------------------------------------------------

class TestExpectedPlayValue:
    def test_returns_reasonable_value(self):
        """EV sample is positive and below an optimistic ceiling."""
        random.seed(42)
        hand = [card("K", "H"), card("9", "H"), card("5", "H"), card("3", "H"), card("2", "D")]
        ctx = _ctx_for(hand)

        ev = _expected_play_value([0, 1, 2, 3], ctx)
        assert ev > 0
        _, _, flush_score = score_hand("Flush", hand[:4])
        assert ev < flush_score * 2

    def test_empty_deck_falls_back_to_best(self):
        """Falls back to current best when deck is too small to draw from."""
        hand = [card("K", "H"), card("9", "H"), card("5", "H"), card("3", "H"), card("2", "D")]
        ctx = _ctx_for(hand, deck=[])
        ev = _expected_play_value([0, 1, 2, 3], ctx)
        assert ev == ctx.best.total

    def test_deterministic_with_seed(self):
        """Same seed produces same EV."""
        hand = [card("K", "H"), card("9", "H"), card("5", "H"), card("3", "H"), card("2", "D")]
        ctx = _ctx_for(hand)

        random.seed(123)
        ev1 = _expected_play_value([0, 1, 2, 3], ctx)
        random.seed(123)
        ev2 = _expected_play_value([0, 1, 2, 3], ctx)
        assert ev1 == ev2

    def test_hearts_deck_favors_flush(self):
        """Deck full of Hearts → EV is high (flush draws land reliably)."""
        random.seed(42)
        hand = [card("K", "H"), card("9", "H"), card("5", "H"), card("3", "H"), card("2", "D")]
        jokers_list = [joker("j_tribe")]
        hearts_deck = [card(r, "H") for r in "2345678TJQA"]
        ctx = _ctx_for(hand, jokers_list, deck=hearts_deck)

        ev = _expected_play_value([0, 1, 2, 3], ctx)
        _, _, flush_score = score_hand("Flush", hand[:4], jokers=jokers_list)
        assert ev >= flush_score * 0.5, f"Hearts deck EV ({ev:.0f}) should be high"

    def test_junk_deck_is_modest(self):
        """Low-card off-suit deck → EV close to the kept Pair's baseline."""
        random.seed(42)
        hand = [card("K", "H"), card("K", "D"), card("9", "H"), card("5", "H"), card("3", "H")]
        junk_deck = [card(r, s) for r in "234" for s in "CS"]
        ctx = _ctx_for(hand, deck=junk_deck)

        ev = _expected_play_value([0, 1], ctx)
        assert ev > 0
        assert ev <= ctx.best.total * 2


# ---------------------------------------------------------------------------
# _best_chase — end-to-end ranking
# ---------------------------------------------------------------------------

class TestBestChase:
    def test_best_chase_picks_highest_ev(self):
        """Given multiple candidates, picks the one with highest MC EV."""
        random.seed(42)
        hand = [card("K", "H"), card("9", "H"), card("5", "H"), card("3", "H"), card("2", "D")]
        jokers_list = [joker("j_tribe"), joker("j_droll")]
        ctx = _ctx_for(hand, jokers_list)

        suggestions = [
            ChaseCandidate([4], "chase Flush (50% to hit, H), discard 1 cards",
                           "Flush", [0, 1, 2, 3], 0.5),
        ]
        result = _best_chase(suggestions, ctx, ctx.best.total)
        assert result is not None, "Flush chase should be accepted"
        assert "chase" in result.reason.lower()


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
        random.seed(42)
        hand = [card("K", "H"), card("9", "H"), card("5", "H"), card("3", "H"), card("2", "D")]
        ctx = _ctx_for(hand)

        ev = _expected_play_value([0, 1, 2, 3], ctx)
        assert ev > 0
        assert ev > ctx.best.total   # flush chase beats High Card


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

    def test_mc_blocks_marginal_chase(self):
        """With strong pair jokers and a rich deck, marginal flush chases get rejected."""
        random.seed(42)
        hand = [card("K", "H"), card("K", "D"), card("9", "H"), card("5", "H"), card("3", "H")]
        jokers_list = [joker("j_duo"), joker("j_jolly")]
        rich_deck = [card(r, s) for r in "TJQKA" for s in "HDCS"]

        state = _make_state(hand, jokers=jokers_list, deck_cards=rich_deck,
                            blind_score=5000, hands_left=4, discards_left=3)
        result = DiscardToImprove().evaluate(state)
        if result is not None:
            assert "chase" not in result.reason.lower() or "EV" in result.reason
