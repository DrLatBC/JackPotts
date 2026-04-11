"""Tests for batch 069 scoring fixes.

Fix 1: Four Fingers straight detection — sliding window in _is_straight()
Fix 2: Four Fingers Flush House/Flush Five scoring cards
Fix 3: Flower Pot debuffed WILD cards
Fix 4: The Ox zeroes money before Bull calculates
Fix 5: Luchador sell clears boss restrictions (context-level)
"""

import math
from dataclasses import replace as dc_replace

from balatro_bot.domain.scoring.classify import (
    classify_hand,
    _is_straight,
    _scoring_cards_for,
)
from balatro_bot.domain.scoring.estimate import score_hand, score_hand_detailed
from balatro_bot.cards import card_rank
from tests.conftest import card, wild_card, stone_card, joker


def _joker_with_ability(key: str, ability: dict, **extra) -> dict:
    j = joker(key)
    j["value"] = {"ability": ability}
    j.update(extra)
    return j


# ---------------------------------------------------------------------------
# Fix 1: Four Fingers straight — sliding window
# ---------------------------------------------------------------------------

class TestFourFingersStraight:
    """_is_straight with four_fingers should find any 4-card run in 5 cards."""

    def test_4567A_is_straight_with_four_fingers(self):
        """The original bug: [4,5,6,7,A] — Ace breaks full-span check."""
        cards = [card("4", "H"), card("5", "D"), card("6", "C"),
                 card("7", "S"), card("A", "H")]
        assert _is_straight(cards, four_fingers=True)

    def test_4567A_classifies_as_straight_with_four_fingers(self):
        cards = [card("4", "H"), card("5", "D"), card("6", "C"),
                 card("7", "S"), card("A", "H")]
        assert classify_hand(cards, four_fingers=True) == "Straight"

    def test_4567A_not_straight_without_four_fingers(self):
        cards = [card("4", "H"), card("5", "D"), card("6", "C"),
                 card("7", "S"), card("A", "H")]
        assert not _is_straight(cards, four_fingers=False)

    def test_2345K_four_fingers(self):
        """Low run with off-suit king."""
        cards = [card("2", "H"), card("3", "D"), card("4", "C"),
                 card("5", "S"), card("K", "H")]
        assert _is_straight(cards, four_fingers=True)

    def test_3_card_not_enough_for_four_fingers(self):
        cards = [card("5", "H"), card("6", "D"), card("7", "C")]
        assert not _is_straight(cards, four_fingers=True)

    def test_ace_low_four_fingers(self):
        """A-2-3-4 with a gap card should still be a straight."""
        cards = [card("A", "H"), card("2", "D"), card("3", "C"),
                 card("4", "S"), card("K", "H")]
        assert _is_straight(cards, four_fingers=True)

    def test_normal_5_card_straight_still_works(self):
        """Regression: standard 5-card straight must still pass."""
        cards = [card("5", "H"), card("6", "D"), card("7", "C"),
                 card("8", "S"), card("9", "H")]
        assert _is_straight(cards, four_fingers=False)

    def test_ace_low_5_card_still_works(self):
        """Regression: A-2-3-4-5 must still be a straight."""
        cards = [card("A", "H"), card("2", "D"), card("3", "C"),
                 card("4", "S"), card("5", "H")]
        assert _is_straight(cards, four_fingers=False)

    def test_four_fingers_straight_flush(self):
        """4-card suited run + off-suit card → Straight Flush with four_fingers."""
        cards = [card("4", "H"), card("5", "H"), card("6", "H"),
                 card("7", "H"), card("A", "S")]
        assert classify_hand(cards, four_fingers=True) == "Straight Flush"

    def test_shortcut_four_fingers(self):
        """Shortcut + Four Fingers: gaps of 2 allowed, only need 4."""
        cards = [card("2", "H"), card("4", "D"), card("6", "C"),
                 card("8", "S"), card("K", "H")]
        assert _is_straight(cards, four_fingers=True, shortcut=True)


# ---------------------------------------------------------------------------
# Fix 1b: Four Fingers Straight scoring cards (only 4 cards score)
# ---------------------------------------------------------------------------

class TestFourFingersStraightScoringCards:
    """With Four Fingers, cards in the straight window score (including dup ranks)."""

    def test_straight_four_fingers_drops_offrank(self):
        """[4,5,6,7,A] — Ace is off-rank, only 4-5-6-7 should score."""
        cards = [card("4", "H"), card("5", "D"), card("6", "C"),
                 card("7", "S"), card("A", "H")]
        scoring = _scoring_cards_for("Straight", cards, four_fingers=True)
        assert len(scoring) == 4, f"Only 4 straight cards should score, got {len(scoring)}"
        scored_ranks = {card_rank(c) for c in scoring}
        assert scored_ranks == {"4", "5", "6", "7"}

    def test_straight_four_fingers_dup_rank_scores(self):
        """[6,7,7,8,9] — both 7s are in the window, both score."""
        cards = [card("6", "H"), card("7", "D"), card("7", "C"),
                 card("8", "S"), card("9", "H")]
        scoring = _scoring_cards_for("Straight", cards, four_fingers=True)
        assert len(scoring) == 5, f"All 5 cards should score (dup rank in window), got {len(scoring)}"

    def test_straight_no_four_fingers_all_score(self):
        """Without Four Fingers, all 5 straight cards score."""
        cards = [card("5", "H"), card("6", "D"), card("7", "C"),
                 card("8", "S"), card("9", "H")]
        scoring = _scoring_cards_for("Straight", cards)
        assert len(scoring) == 5


# ---------------------------------------------------------------------------
# Fix 2: Four Fingers Flush House / Flush Five scoring cards
# ---------------------------------------------------------------------------

class TestFourFingersFlushHouseScoringCards:
    """Flush House/Five with four_fingers should score ALL 5 cards."""

    def test_flush_house_four_fingers_all_cards_score(self):
        """Off-suit Full House member must not be dropped."""
        cards = [
            wild_card("K", "H"), wild_card("K", "D"), wild_card("K", "C"),
            card("9", "H"), card("9", "D"),  # 9♦ is off-suit from hearts
        ]
        assert classify_hand(cards, four_fingers=True) == "Flush House"
        scoring = _scoring_cards_for("Flush House", cards, four_fingers=True)
        assert len(scoring) == 5, f"All 5 cards should score, got {len(scoring)}"

    def test_flush_five_four_fingers_all_cards_score(self):
        """Flush Five with four_fingers: all 5 cards must score."""
        cards = [
            wild_card("7", "H"), wild_card("7", "D"), card("7", "H"),
            card("7", "H"), card("7", "H"),
        ]
        assert classify_hand(cards, four_fingers=True) == "Flush Five"
        scoring = _scoring_cards_for("Flush Five", cards, four_fingers=True)
        assert len(scoring) == 5

    def test_straight_flush_four_fingers_drops_offrank_offsuit(self):
        """SF with Four Fingers: A♠ is neither in flush nor straight → dropped."""
        cards = [card("4", "H"), card("5", "H"), card("6", "H"),
                 card("7", "H"), card("A", "S")]
        scoring = _scoring_cards_for("Straight Flush", cards, four_fingers=True)
        assert len(scoring) == 4, f"Only 4-card SF should score, got {len(scoring)}"
        scored_ranks = {card_rank(c) for c in scoring}
        assert "A" not in scored_ranks

    def test_straight_flush_four_fingers_flush_only_card_scores(self):
        """SF with Four Fingers: K♥ is flush-only (not in straight) → scores."""
        cards = [card("4", "H"), card("5", "H"), card("6", "H"),
                 card("7", "H"), card("K", "H")]
        scoring = _scoring_cards_for("Straight Flush", cards, four_fingers=True)
        assert len(scoring) == 5, f"All 5 should score (K is in flush), got {len(scoring)}"

    def test_straight_flush_four_fingers_disjoint_all_score(self):
        """SF with Four Fingers: flush/straight subsets differ → union scores."""
        # Flush={4♥,5♥,7♥,8♥}, Straight={5,6,7,8}. Union=all 5.
        cards = [card("4", "H"), card("5", "H"), card("6", "S"),
                 card("7", "H"), card("8", "H")]
        scoring = _scoring_cards_for("Straight Flush", cards, four_fingers=True)
        assert len(scoring) == 5, f"All 5 should score (union), got {len(scoring)}"

    def test_plain_flush_four_fingers_drops_offsuit(self):
        """Plain Flush with four_fingers should still drop the off-suit card."""
        cards = [card("2", "H"), card("5", "H"), card("8", "H"),
                 card("J", "H"), card("A", "S")]
        scoring = _scoring_cards_for("Flush", cards, four_fingers=True)
        # Only 4 hearts should score
        assert len(scoring) == 4


# ---------------------------------------------------------------------------
# Fix 3: Flower Pot debuffed WILD cards
# ---------------------------------------------------------------------------

class TestFlowerPotDebuffedWild:
    """Debuffed WILD cards contribute NOTHING to Flower Pot suit check."""

    def _flower_pot(self):
        return _joker_with_ability("j_flower_pot", {"extra": 3.0})

    def test_debuffed_wild_does_not_count(self):
        """Debuffed WILD should not fill a missing suit for Flower Pot."""
        cards = [
            card("A", "H"),   # Heart
            card("K", "D"),   # Diamond
            card("Q", "C"),   # Club
            card("J", "H"),   # Heart (no Spade)
            card("T", "H"),   # Heart
        ]
        # Make J♥ a debuffed WILD — should NOT fill the missing Spade
        cards[3] = dc_replace(cards[3],
                              modifier=dc_replace(cards[3].modifier, enhancement="WILD"),
                              state=dc_replace(cards[3].state, debuff=True))

        _, _, total_with = score_hand("Straight", cards, jokers=[self._flower_pot()])
        _, _, total_without = score_hand("Straight", cards)
        assert total_with == total_without, \
            "Debuffed WILD should not trigger Flower Pot"

    def test_non_debuffed_wild_fills_suit(self):
        """Non-debuffed WILD should fill a missing suit for Flower Pot."""
        cards = [
            card("A", "H"),
            card("K", "D"),
            card("Q", "C"),
            wild_card("J", "H"),  # WILD fills Spade
            card("T", "H"),
        ]
        _, _, total_with = score_hand("Straight", cards, jokers=[self._flower_pot()])
        _, _, total_without = score_hand("Straight", cards)
        assert total_with > total_without, \
            "Non-debuffed WILD should trigger Flower Pot"

    def test_debuffed_non_wild_still_contributes_suit(self):
        """A debuffed normal card's suit still counts for Flower Pot."""
        cards = [
            card("A", "H"),
            card("K", "D"),
            card("Q", "C"),
            card("J", "S"),   # natural Spade
            card("T", "H"),
        ]
        cards[3] = dc_replace(cards[3], state=dc_replace(cards[3].state, debuff=True))
        _, _, total_with = score_hand("Straight", cards, jokers=[self._flower_pot()])
        _, _, total_without = score_hand("Straight", cards)
        assert total_with > total_without, \
            "Debuffed normal card should still contribute suit to Flower Pot"


# ---------------------------------------------------------------------------
# Fix 4: The Ox zeroes money before Bull
# ---------------------------------------------------------------------------

class TestOxBull:
    """Bull uses $0 when The Ox fires (playing most-played hand type)."""

    def _bull_joker(self):
        return _joker_with_ability("j_bull", {"extra": 2})

    def test_bull_with_ox_most_played_zeros_money(self):
        """Playing the most-played hand under The Ox → Bull gets $0."""
        cards = [card("K", "H"), card("K", "D"), card("3", "C")]
        levels = {"Pair": {"chips": 10, "mult": 2, "level": 1, "played": 5},
                  "High Card": {"chips": 5, "mult": 1, "level": 1, "played": 2}}
        _, _, total_ox = score_hand(
            "Pair", cards, hand_levels=levels,
            jokers=[self._bull_joker()], money=100, blind_name="The Ox",
        )
        _, _, total_no_ox = score_hand(
            "Pair", cards, hand_levels=levels,
            jokers=[self._bull_joker()], money=100, blind_name="",
        )
        # With The Ox, money should be zeroed → Bull adds 0 chips
        # Without The Ox, Bull adds 2 * 100 = 200 chips
        assert total_ox < total_no_ox, \
            f"The Ox should zero money for Bull: ox={total_ox}, no_ox={total_no_ox}"

    def test_bull_with_ox_non_most_played_keeps_money(self):
        """Playing a non-most-played hand under The Ox → Bull keeps money."""
        cards = [card("K", "H"), card("K", "D"), card("3", "C")]
        levels = {"Pair": {"chips": 10, "mult": 2, "level": 1, "played": 2},
                  "High Card": {"chips": 5, "mult": 1, "level": 1, "played": 5}}
        _, _, total_ox = score_hand(
            "Pair", cards, hand_levels=levels,
            jokers=[self._bull_joker()], money=100, blind_name="The Ox",
        )
        _, _, total_no_ox = score_hand(
            "Pair", cards, hand_levels=levels,
            jokers=[self._bull_joker()], money=100, blind_name="",
        )
        # Pair has played=2, High Card has played=5 → Pair is not most-played
        assert total_ox == total_no_ox, \
            "Non-most-played hand under The Ox should keep money"

    def test_ox_detailed_also_zeros(self):
        """score_hand_detailed should also apply The Ox money zeroing."""
        cards = [card("K", "H"), card("K", "D"), card("3", "C")]
        levels = {"Pair": {"chips": 10, "mult": 2, "level": 1, "played": 5},
                  "High Card": {"chips": 5, "mult": 1, "level": 1, "played": 2}}
        detail = score_hand_detailed(
            "Pair", cards, hand_levels=levels,
            jokers=[self._bull_joker()], money=100, blind_name="The Ox",
        )
        detail_no_ox = score_hand_detailed(
            "Pair", cards, hand_levels=levels,
            jokers=[self._bull_joker()], money=100, blind_name="",
        )
        assert detail["total"] < detail_no_ox["total"]

    def test_ox_locked_hand_doesnt_shift(self):
        """ox_most_played locks at blind start — even if played counts shift."""
        cards = [card("K", "H"), card("K", "D"), card("3", "C")]
        # Pair was most-played (5) at blind start, but now HC has been played
        # more during the round. The locked hand should still be Pair.
        levels = {"Pair": {"chips": 10, "mult": 2, "level": 1, "played": 5},
                  "High Card": {"chips": 5, "mult": 1, "level": 1, "played": 8}}
        # With ox_most_played="Pair", playing Pair should zero money
        _, _, total_locked = score_hand(
            "Pair", cards, hand_levels=levels,
            jokers=[self._bull_joker()], money=100, blind_name="The Ox",
            ox_most_played="Pair",
        )
        # Without explicit lock, fallback would pick HC (played=8) as most-played
        # and Pair would NOT zero money — that's the bug we're preventing
        _, _, total_fallback = score_hand(
            "Pair", cards, hand_levels=levels,
            jokers=[self._bull_joker()], money=100, blind_name="The Ox",
        )
        # Locked version should zero money (lower score)
        _, _, total_no_ox = score_hand(
            "Pair", cards, hand_levels=levels,
            jokers=[self._bull_joker()], money=100, blind_name="",
        )
        assert total_locked < total_no_ox, "Locked Ox should zero money for Pair"
        assert total_fallback == total_no_ox, \
            "Fallback (HC most-played) should NOT zero money for Pair"


# ---------------------------------------------------------------------------
# Fix 5: Luchador sell clears boss restrictions (context-level)
# ---------------------------------------------------------------------------

class TestLuchadorBossDisable:
    """Boss restrictions should not apply when boss_disabled is True."""

    def _make_snapshot(self, blind_name, boss_disabled=False):
        from balatro_bot.domain.models.snapshot import (
            BlindSnapshot, RoundSnapshot, Snapshot,
        )
        return Snapshot(
            state_name="SELECTING_HAND",
            seed="TEST",
            ante=3,
            round_num=3,
            money=50,
            joker_limit=5,
            deck_count=40,
            round=RoundSnapshot(chips=0, hands_left=4, discards_left=3, ancient_suit=None),
            current_blind=BlindSnapshot(
                key="bl_boss", name=blind_name, score=10000,
                status="CURRENT", boss_disabled=boss_disabled,
            ),
            hand_cards=[
                card("A", "H"), card("K", "H"), card("Q", "H"),
                card("J", "H"), card("T", "H"),
                card("9", "D"), card("8", "C"), card("7", "S"),
            ],
            hand_levels={
                "Flush": {"chips": 35, "mult": 4, "level": 1, "played_this_round": 1},
                "Straight": {"chips": 30, "mult": 4, "level": 1},
                "High Card": {"chips": 5, "mult": 1, "level": 1},
                "Pair": {"chips": 10, "mult": 2, "level": 1},
                "Straight Flush": {"chips": 100, "mult": 8, "level": 1},
            },
            jokers=[],
            deck_cards=[card("2", "S")] * 30,
            consumables=[],
            shop_cards=[],
            vouchers=[],
            pack_cards=[],
        )

    def test_mouth_locked_when_active(self):
        """The Mouth should lock hand type when boss is active."""
        from balatro_bot.context import RoundContext
        snap = self._make_snapshot("The Mouth", boss_disabled=False)
        ctx = RoundContext.from_snapshot(snap)
        assert ctx.mouth_locked_hand == "Flush"

    def test_mouth_unlocked_when_boss_disabled(self):
        """The Mouth lock should be cleared when boss is disabled."""
        from balatro_bot.context import RoundContext
        snap = self._make_snapshot("The Mouth", boss_disabled=True)
        ctx = RoundContext.from_snapshot(snap)
        assert ctx.mouth_locked_hand is None

    def test_eye_restrictions_cleared_when_disabled(self):
        """The Eye's excluded hands should be None when boss is disabled."""
        from balatro_bot.context import RoundContext
        snap = self._make_snapshot("The Eye", boss_disabled=True)
        ctx = RoundContext.from_snapshot(snap)
        assert ctx.eye_used_hands is None

    def test_psychic_min_cards_cleared_when_disabled(self):
        """The Psychic's 5-card minimum should revert to 1."""
        from balatro_bot.context import RoundContext
        snap = self._make_snapshot("The Psychic", boss_disabled=True)
        ctx = RoundContext.from_snapshot(snap)
        assert ctx.min_cards == 1

    def test_head_debuff_cleared_when_disabled(self):
        """The Head's suit debuff should not apply when disabled."""
        from balatro_bot.context import RoundContext
        snap = self._make_snapshot("The Head", boss_disabled=True)
        ctx = RoundContext.from_snapshot(snap)
        assert ctx.scoring_suit is None

    def test_crimson_heart_discount_cleared_when_disabled(self):
        """Crimson Heart's score discount should revert to 1.0."""
        from balatro_bot.context import RoundContext
        snap = self._make_snapshot("Crimson Heart", boss_disabled=True)
        ctx = RoundContext.from_snapshot(snap)
        assert ctx.score_discount == 1.0

    def test_boss_disabled_propagates_through_state_adapter(self):
        """_boss_disabled flag on raw state dict reaches BlindSnapshot."""
        from balatro_bot.infrastructure.state_adapter import adapt_state
        state = {
            "state": "SELECTING_HAND",
            "seed": "TEST",
            "ante_num": 3,
            "round_num": 3,
            "money": 50,
            "blinds": {
                "boss": {"key": "bl_mouth", "name": "The Mouth",
                         "score": 10000, "status": "CURRENT"},
            },
            "round": {"chips": 0, "hands_left": 4, "discards_left": 3},
            "hand": {"cards": []},
            "hands": {},
            "jokers": {"cards": [], "limit": 5},
            "cards": {"count": 40, "cards": []},
            "consumables": {"cards": []},
            "shop": {"cards": []},
            "vouchers": {"cards": []},
            "pack": {"cards": []},
            "_boss_disabled": True,
        }
        snap = adapt_state(state)
        assert snap.current_blind.boss_disabled is True
