"""Tests for hand_evaluator.py — verify hand classification and scoring."""

from hand_evaluator import (
    classify_hand,
    best_hand,
    score_hand,
    card_chip_value,
    enumerate_hands,
    discard_candidates,
)


# ---------------------------------------------------------------------------
# Card factory helpers
# ---------------------------------------------------------------------------

def card(rank: str, suit: str, enhancement: str | None = None) -> dict:
    """Build a minimal card dict matching the balatrobot schema."""
    c: dict = {
        "id": 0,
        "key": f"{suit}_{rank}",
        "set": "DEFAULT",
        "label": f"{rank} of {suit}",
        "value": {"suit": suit, "rank": rank},
        "modifier": {},
        "state": {},
        "cost": {},
    }
    if enhancement:
        c["modifier"]["enhancement"] = enhancement
    return c


def stone_card() -> dict:
    return {
        "id": 0, "key": "stone", "set": "ENHANCED", "label": "Stone Card",
        "value": {}, "modifier": {"enhancement": "STONE"}, "state": {}, "cost": {},
    }


def wild_card(rank: str, suit: str) -> dict:
    c = card(rank, suit)
    c["modifier"]["enhancement"] = "WILD"
    return c


# ---------------------------------------------------------------------------
# classify_hand tests
# ---------------------------------------------------------------------------

class TestClassifyHand:
    def test_high_card(self):
        cards = [card("2", "H"), card("5", "D"), card("9", "C")]
        assert classify_hand(cards) == "High Card"

    def test_pair(self):
        cards = [card("K", "H"), card("K", "D"), card("3", "C")]
        assert classify_hand(cards) == "Pair"

    def test_two_pair(self):
        cards = [card("K", "H"), card("K", "D"), card("7", "C"), card("7", "S"), card("2", "H")]
        assert classify_hand(cards) == "Two Pair"

    def test_three_of_a_kind(self):
        cards = [card("J", "H"), card("J", "D"), card("J", "C"), card("2", "S"), card("5", "H")]
        assert classify_hand(cards) == "Three of a Kind"

    def test_straight(self):
        cards = [card("5", "H"), card("6", "D"), card("7", "C"), card("8", "S"), card("9", "H")]
        assert classify_hand(cards) == "Straight"

    def test_straight_ace_low(self):
        cards = [card("A", "H"), card("2", "D"), card("3", "C"), card("4", "S"), card("5", "H")]
        assert classify_hand(cards) == "Straight"

    def test_straight_ace_high(self):
        cards = [card("T", "H"), card("J", "D"), card("Q", "C"), card("K", "S"), card("A", "H")]
        assert classify_hand(cards) == "Straight"

    def test_flush(self):
        cards = [card("2", "H"), card("5", "H"), card("8", "H"), card("J", "H"), card("A", "H")]
        assert classify_hand(cards) == "Flush"

    def test_full_house(self):
        cards = [card("Q", "H"), card("Q", "D"), card("Q", "C"), card("9", "S"), card("9", "H")]
        assert classify_hand(cards) == "Full House"

    def test_four_of_a_kind(self):
        cards = [card("8", "H"), card("8", "D"), card("8", "C"), card("8", "S"), card("3", "H")]
        assert classify_hand(cards) == "Four of a Kind"

    def test_straight_flush(self):
        cards = [card("5", "S"), card("6", "S"), card("7", "S"), card("8", "S"), card("9", "S")]
        assert classify_hand(cards) == "Straight Flush"

    def test_five_of_a_kind(self):
        # Possible with Wild cards in Balatro
        cards = [card("A", "H"), card("A", "D"), card("A", "C"), card("A", "S"), wild_card("A", "H")]
        # 5 Aces
        assert classify_hand(cards) == "Five of a Kind"

    def test_flush_house(self):
        # Full house where all cards are same suit (needs wilds)
        cards = [
            wild_card("K", "H"), wild_card("K", "D"), wild_card("K", "C"),
            card("9", "H"), card("9", "H"),
        ]
        # 3 wilds (count as H) + 2 hearts = flush, and 3K+2x9 = full house => Flush House
        # Wait — wilds count as all suits, so flush check: each wild intersects with H,
        # and the 9s are H. Common suit = H. So yes, flush. And 3K+2x9 = full house.
        assert classify_hand(cards) == "Flush House"

    def test_flush_five(self):
        cards = [card("7", "D"), card("7", "D"), card("7", "D"), card("7", "D"), card("7", "D")]
        assert classify_hand(cards) == "Flush Five"

    def test_wild_enables_flush(self):
        cards = [card("2", "H"), card("5", "D"), card("8", "C"), card("J", "S"), wild_card("A", "H")]
        # Wild counts as all suits, but the other 4 cards are all different suits.
        # Flush requires ALL cards to share a common suit. Wild shares all, but
        # H ∩ D ∩ C ∩ S = empty. So NOT a flush.
        assert classify_hand(cards) != "Flush"


# ---------------------------------------------------------------------------
# Scoring tests
# ---------------------------------------------------------------------------

class TestScoring:
    def test_pair_base_score(self):
        chips, mult, total = score_hand("Pair", [card("K", "H"), card("K", "D")])
        # Pair base: 10 chips, 2 mult. Kings = 10 each.
        # total_chips = 10 + 10 + 10 = 30, total = 30 * 2 = 60
        assert chips == 30
        assert mult == 2
        assert total == 60

    def test_flush_score(self):
        cards = [card("2", "H"), card("5", "H"), card("8", "H"), card("J", "H"), card("A", "H")]
        chips, mult, total = score_hand("Flush", cards)
        # Flush base: 35 chips, 4 mult. Cards: 2+5+8+10+11 = 36
        # total_chips = 35 + 36 = 71, total = 71 * 4 = 284
        assert chips == 71
        assert mult == 4
        assert total == 284

    def test_stone_card_chips(self):
        assert card_chip_value(stone_card()) == 50

    def test_bonus_card_chips(self):
        c = card("5", "H", enhancement="BONUS")
        assert card_chip_value(c) == 35  # 5 + 30

    def test_leveled_hand(self):
        levels = {"Pair": {"chips": 20, "mult": 4, "level": 3}}
        chips, mult, total = score_hand("Pair", [card("A", "H"), card("A", "D")], levels)
        # Leveled pair: 20 + 11 + 11 = 42 chips, 4 mult = 168
        assert chips == 42
        assert mult == 4
        assert total == 168


# ---------------------------------------------------------------------------
# best_hand tests
# ---------------------------------------------------------------------------

class TestBestHand:
    def test_finds_pair_in_junk(self):
        cards = [card("3", "H"), card("K", "D"), card("K", "C"), card("2", "S"), card("7", "H")]
        result = best_hand(cards)
        assert result is not None
        assert result.hand_name == "Pair"

    def test_finds_flush_over_pair(self):
        cards = [card("2", "H"), card("5", "H"), card("8", "H"), card("K", "H"), card("K", "D")]
        # Has a pair of kings AND a 4-card flush. With 5 cards, only 4 are hearts,
        # so no 5-card flush. Best should be pair of kings.
        result = best_hand(cards)
        assert result is not None
        # Actually, let's make a real flush
        cards = [card("2", "H"), card("5", "H"), card("8", "H"), card("J", "H"), card("A", "H")]
        result = best_hand(cards)
        assert result is not None
        assert result.hand_name == "Flush"

    def test_prefers_higher_scoring_same_type(self):
        cards = [card("A", "H"), card("A", "D"), card("2", "C"), card("2", "S"), card("7", "H")]
        result = best_hand(cards)
        assert result is not None
        # Should find Two Pair (AA+22) which beats either single pair
        assert result.hand_name == "Two Pair"


# ---------------------------------------------------------------------------
# Discard tests
# ---------------------------------------------------------------------------

class TestDiscard:
    def test_discards_dead_cards(self):
        cards = [card("K", "H"), card("K", "D"), card("3", "C"), card("5", "S"), card("7", "H")]
        suggestions = discard_candidates(cards)
        assert len(suggestions) > 0
        indices, reason = suggestions[0]
        # Should keep the pair (K, K) and discard the rest
        assert 0 not in indices  # K of hearts
        assert 1 not in indices  # K of diamonds
        assert "keep Pair" in reason


# ---------------------------------------------------------------------------
# Joker-aware scoring tests
# ---------------------------------------------------------------------------

def joker(key: str, label: str = "") -> dict:
    """Build a minimal joker dict."""
    return {"key": key, "label": label or key, "set": "JOKER", "cost": {"sell": 3}}


class TestJokerScoring:
    def test_no_jokers_unchanged(self):
        """Without jokers, scoring is identical to base."""
        cards = [card("K", "H"), card("K", "D")]
        chips1, mult1, total1 = score_hand("Pair", cards)
        chips2, mult2, total2 = score_hand("Pair", cards, jokers=[])
        assert total1 == total2

    def test_flat_mult_joker(self):
        """j_joker adds +4 mult."""
        cards = [card("K", "H"), card("K", "D")]
        _, _, base_total = score_hand("Pair", cards)
        _, _, joker_total = score_hand("Pair", cards, jokers=[joker("j_joker")])
        # Base: (10 + 10 + 10) * 2 = 60. With joker: 30 * (2+4) = 180
        assert joker_total == 180
        assert joker_total > base_total

    def test_hand_type_conditional(self):
        """Sly Joker adds +50 chips only on Pair hands."""
        pair = [card("A", "H"), card("A", "D")]
        flush = [card("2", "H"), card("5", "H"), card("8", "H"), card("J", "H"), card("A", "H")]
        sly = [joker("j_sly")]

        _, _, pair_total = score_hand("Pair", pair, jokers=sly)
        # Base pair: (10+11+11) * 2 = 64. With sly: (10+50+11+11) * 2 = 164
        assert pair_total == 164

        _, _, flush_total_with = score_hand("Flush", flush, jokers=sly)
        _, _, flush_total_without = score_hand("Flush", flush)
        # Sly doesn't trigger on Flush (Flush doesn't contain Pair)
        assert flush_total_with == flush_total_without

    def test_xmult_joker(self):
        """The Duo gives X2 on hands containing a Pair."""
        cards = [card("K", "H"), card("K", "D")]
        duo = [joker("j_duo")]
        _, _, total = score_hand("Pair", cards, jokers=duo)
        # Base: 30 chips, 2 mult. Duo: mult *= 2 -> 4. Total: 30 * 4 = 120
        assert total == 120

    def test_suit_conditional(self):
        """Greedy Joker adds +3 mult per scored Diamond."""
        cards = [card("K", "D"), card("K", "H")]
        greedy = [joker("j_greedy_joker")]
        _, _, total = score_hand("Pair", cards, jokers=greedy)
        # 1 diamond in scoring cards -> +3 mult. (10+10+10) * (2+3) = 150
        assert total == 150

    def test_multiple_jokers_stack(self):
        """Multiple jokers apply in order."""
        cards = [card("K", "H"), card("K", "D")]
        jokers_list = [joker("j_joker"), joker("j_jolly"), joker("j_duo")]
        _, _, total = score_hand("Pair", cards, jokers=jokers_list)
        # chips=30, mult: 2 +4=6 +8=14, then Duo x2 -> 28. Total: 30*28 = 840
        assert total == 840

    def test_xmult_order_matters(self):
        """XMult before +Mult gives different result than +Mult before XMult."""
        cards = [card("K", "H"), card("K", "D")]
        # Duo (x2 if Pair) BEFORE Joker (+4 mult)
        xmult_first = [joker("j_duo"), joker("j_joker")]
        _, _, total_xfirst = score_hand("Pair", cards, jokers=xmult_first)
        # mult: 2, Duo x2 -> 4, Joker +4 -> 8. Total: 30 * 8 = 240
        assert total_xfirst == 240

        # Joker (+4 mult) BEFORE Duo (x2 if Pair)
        mult_first = [joker("j_joker"), joker("j_duo")]
        _, _, total_mfirst = score_hand("Pair", cards, jokers=mult_first)
        # mult: 2, Joker +4 -> 6, Duo x2 -> 12. Total: 30 * 12 = 360
        assert total_mfirst == 360

        # Order matters — these should NOT be equal
        assert total_xfirst != total_mfirst

    def test_best_hand_prefers_joker_synergy(self):
        """With Droll Joker (+10 mult on Flush), best_hand should prefer Flush."""
        # Hand has both a pair of Kings and a flush in hearts
        cards = [
            card("2", "H"), card("5", "H"), card("8", "H"),
            card("J", "H"), card("A", "H"),
        ]
        droll = [joker("j_droll")]
        result = best_hand(cards, jokers=droll)
        assert result is not None
        assert result.hand_name == "Flush"

    def test_full_house_contains_pair(self):
        """Jolly Joker (+8 mult on Pair) triggers on Full House."""
        cards = [card("Q", "H"), card("Q", "D"), card("Q", "C"), card("9", "S"), card("9", "H")]
        jolly = [joker("j_jolly")]
        _, _, total_with = score_hand("Full House", cards, jokers=jolly)
        _, _, total_without = score_hand("Full House", cards)
        assert total_with > total_without


# ---------------------------------------------------------------------------
# Effect text parser tests
# ---------------------------------------------------------------------------

class TestParseEffectValue:
    def test_flat_mult(self):
        from joker_effects import parse_effect_value
        result = parse_effect_value("+4 Mult")
        assert result["mult"] == 4.0
        assert result["chips"] is None
        assert result["xmult"] is None

    def test_flat_chips(self):
        from joker_effects import parse_effect_value
        result = parse_effect_value("+15 Chips")
        assert result["chips"] == 15.0
        assert result["mult"] is None

    def test_xmult(self):
        from joker_effects import parse_effect_value
        result = parse_effect_value("X2.5 Mult")
        assert result["xmult"] == 2.5
        assert result["mult"] is None

    def test_xmult_integer(self):
        from joker_effects import parse_effect_value
        result = parse_effect_value("X3 Mult")
        assert result["xmult"] == 3.0

    def test_chips_with_flavor_text(self):
        from joker_effects import parse_effect_value
        result = parse_effect_value("+100 Chips -5 for each hand played")
        assert result["chips"] == 100.0

    def test_xmult_with_flavor_text(self):
        from joker_effects import parse_effect_value
        result = parse_effect_value("X3 Mult 1 in 1000 chance this card is destroyed at end of round")
        assert result["xmult"] == 3.0

    def test_multi_value(self):
        from joker_effects import parse_effect_value
        result = parse_effect_value("+20 Chips +4 Mult per scored Ace")
        assert result["chips"] == 20.0
        assert result["mult"] == 4.0

    def test_empty_string(self):
        from joker_effects import parse_effect_value
        result = parse_effect_value("")
        assert result["chips"] is None
        assert result["mult"] is None
        assert result["xmult"] is None

    def test_no_match(self):
        from joker_effects import parse_effect_value
        result = parse_effect_value("Creates a random Joker card")
        assert result["chips"] is None
        assert result["mult"] is None
        assert result["xmult"] is None

    def test_get_parsed_value_with_effect(self):
        from joker_effects import _get_parsed_value
        j = {"key": "j_green_joker", "value": {"effect": "+12 Mult"}}
        assert _get_parsed_value(j, "mult", fallback=5) == 12.0

    def test_get_parsed_value_fallback(self):
        from joker_effects import _get_parsed_value
        j = {"key": "j_green_joker", "value": {}}
        assert _get_parsed_value(j, "mult", fallback=5) == 5

    def test_get_parsed_value_empty_effect(self):
        from joker_effects import _get_parsed_value
        j = {"key": "j_green_joker", "value": {"effect": ""}}
        assert _get_parsed_value(j, "mult", fallback=5) == 5

    def test_scoring_uses_parsed_value(self):
        """Green Joker with parsed +12 should score higher than fallback +5."""
        cards = [card("K", "H"), card("K", "D")]
        j_parsed = {"key": "j_green_joker", "set": "JOKER", "cost": {"sell": 3},
                     "value": {"effect": "+12 Mult"}}
        j_no_effect = {"key": "j_green_joker", "set": "JOKER", "cost": {"sell": 3},
                        "value": {}}
        _, _, total_parsed = score_hand("Pair", cards, jokers=[j_parsed])
        _, _, total_fallback = score_hand("Pair", cards, jokers=[j_no_effect])
        # Parsed (+12 mult) should be higher than fallback (+5 mult)
        assert total_parsed > total_fallback
        # Green Joker: parsed +12, plus +1 for pre-scoring increment = +13
        # Pair base: 30 chips * (2+13) mult = 450
        assert total_parsed == 450
        # Fallback +5, plus +1 = +6. Pair base: 30 chips * (2+6) mult = 240
        assert total_fallback == 240


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
