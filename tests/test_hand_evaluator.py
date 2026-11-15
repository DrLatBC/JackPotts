"""Hand classification and scoring (domain.scoring classify / estimate / search)."""

from balatro_bot.domain.scoring.classify import classify_hand
from balatro_bot.domain.scoring.estimate import score_hand
from balatro_bot.domain.scoring.search import (
    best_hand,
    enumerate_hands,
    discard_candidates,
)
from balatro_bot.cards import card_chip_value
from tests.conftest import card, stone_card, wild_card, joker


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
        cards = [card("A", "H"), card("A", "D"), card("A", "C"), card("A", "S"), wild_card("A", "H")]
        assert classify_hand(cards) == "Five of a Kind"

    def test_flush_house(self):
        cards = [
            wild_card("K", "H"), wild_card("K", "D"), wild_card("K", "C"),
            card("9", "H"), card("9", "H"),
        ]
        assert classify_hand(cards) == "Flush House"

    def test_flush_five(self):
        cards = [card("7", "D"), card("7", "D"), card("7", "D"), card("7", "D"), card("7", "D")]
        assert classify_hand(cards) == "Flush Five"

    def test_wild_enables_flush(self):
        cards = [card("2", "H"), card("5", "D"), card("8", "C"), card("J", "S"), wild_card("A", "H")]
        assert classify_hand(cards) != "Flush"


# ---------------------------------------------------------------------------
# Scoring tests
# ---------------------------------------------------------------------------

class TestScoring:
    def test_pair_base_score(self):
        chips, mult, total = score_hand("Pair", [card("K", "H"), card("K", "D")])
        assert chips == 30
        assert mult == 2
        assert total == 60

    def test_flush_score(self):
        cards = [card("2", "H"), card("5", "H"), card("8", "H"), card("J", "H"), card("A", "H")]
        chips, mult, total = score_hand("Flush", cards)
        assert chips == 71
        assert mult == 4
        assert total == 284

    def test_stone_card_chips(self):
        assert card_chip_value(stone_card()) == 50

    def test_bonus_card_chips(self):
        c = card("5", "H", enhancement="BONUS")
        assert card_chip_value(c) == 35

    def test_leveled_hand(self):
        levels = {"Pair": {"chips": 20, "mult": 4, "level": 3}}
        chips, mult, total = score_hand("Pair", [card("A", "H"), card("A", "D")], levels)
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
        cards = [card("2", "H"), card("5", "H"), card("8", "H"), card("J", "H"), card("A", "H")]
        result = best_hand(cards)
        assert result is not None
        assert result.hand_name == "Flush"

    def test_prefers_higher_scoring_same_type(self):
        cards = [card("A", "H"), card("A", "D"), card("2", "C"), card("2", "S"), card("7", "H")]
        result = best_hand(cards)
        assert result is not None
        assert result.hand_name == "Two Pair"


# ---------------------------------------------------------------------------
# Discard tests
# ---------------------------------------------------------------------------

class TestDiscard:
    def test_discards_dead_cards(self):
        cards = [card("K", "H"), card("K", "D"), card("3", "C"), card("5", "S"), card("7", "H")]
        suggestions = discard_candidates(cards)
        assert len(suggestions) > 0
        candidate = suggestions[0]
        assert 0 not in candidate.discard_indices
        assert 1 not in candidate.discard_indices
        assert "keep Pair" in candidate.reason


# ---------------------------------------------------------------------------
# Joker-aware scoring tests
# ---------------------------------------------------------------------------

class TestJokerScoring:
    def test_no_jokers_unchanged(self):
        cards = [card("K", "H"), card("K", "D")]
        chips1, mult1, total1 = score_hand("Pair", cards)
        chips2, mult2, total2 = score_hand("Pair", cards, jokers=[])
        assert total1 == total2

    def test_flat_mult_joker(self):
        cards = [card("K", "H"), card("K", "D")]
        _, _, base_total = score_hand("Pair", cards)
        _, _, joker_total = score_hand("Pair", cards, jokers=[joker("j_joker")])
        assert joker_total == 180
        assert joker_total > base_total

    def test_hand_type_conditional(self):
        pair = [card("A", "H"), card("A", "D")]
        flush = [card("2", "H"), card("5", "H"), card("8", "H"), card("J", "H"), card("A", "H")]
        sly = [joker("j_sly")]

        _, _, pair_total = score_hand("Pair", pair, jokers=sly)
        assert pair_total == 164

        _, _, flush_total_with = score_hand("Flush", flush, jokers=sly)
        _, _, flush_total_without = score_hand("Flush", flush)
        assert flush_total_with == flush_total_without

    def test_xmult_joker(self):
        cards = [card("K", "H"), card("K", "D")]
        duo = [joker("j_duo")]
        _, _, total = score_hand("Pair", cards, jokers=duo)
        assert total == 120

    def test_suit_conditional(self):
        cards = [card("K", "D"), card("K", "H")]
        greedy = [joker("j_greedy_joker")]
        _, _, total = score_hand("Pair", cards, jokers=greedy)
        assert total == 150

    def test_multiple_jokers_stack(self):
        cards = [card("K", "H"), card("K", "D")]
        jokers_list = [joker("j_joker"), joker("j_jolly"), joker("j_duo")]
        _, _, total = score_hand("Pair", cards, jokers=jokers_list)
        assert total == 840

    def test_xmult_order_matters(self):
        cards = [card("K", "H"), card("K", "D")]
        xmult_first = [joker("j_duo"), joker("j_joker")]
        _, _, total_xfirst = score_hand("Pair", cards, jokers=xmult_first)
        assert total_xfirst == 240

        mult_first = [joker("j_joker"), joker("j_duo")]
        _, _, total_mfirst = score_hand("Pair", cards, jokers=mult_first)
        assert total_mfirst == 360

        assert total_xfirst != total_mfirst

    def test_best_hand_prefers_joker_synergy(self):
        cards = [
            card("2", "H"), card("5", "H"), card("8", "H"),
            card("J", "H"), card("A", "H"),
        ]
        droll = [joker("j_droll")]
        result = best_hand(cards, jokers=droll)
        assert result is not None
        assert result.hand_name == "Flush"

    def test_full_house_contains_pair(self):
        cards = [card("Q", "H"), card("Q", "D"), card("Q", "C"), card("9", "S"), card("9", "H")]
        jolly = [joker("j_jolly")]
        _, _, total_with = score_hand("Full House", cards, jokers=jolly)
        _, _, total_without = score_hand("Full House", cards)
        assert total_with > total_without


# ---------------------------------------------------------------------------
# Effect text parser (logs) and current-value accessor tests
# ---------------------------------------------------------------------------

class TestParseEffectValue:
    def test_flat_mult(self):
        from balatro_bot.joker_effects import parse_effect_value
        result = parse_effect_value("+4 Mult")
        assert result["mult"] == 4.0
        assert result["chips"] is None
        assert result["xmult"] is None

    def test_flat_chips(self):
        from balatro_bot.joker_effects import parse_effect_value
        result = parse_effect_value("+15 Chips")
        assert result["chips"] == 15.0
        assert result["mult"] is None

    def test_xmult(self):
        from balatro_bot.joker_effects import parse_effect_value
        result = parse_effect_value("X2.5 Mult")
        assert result["xmult"] == 2.5
        assert result["mult"] is None

    def test_xmult_integer(self):
        from balatro_bot.joker_effects import parse_effect_value
        result = parse_effect_value("X3 Mult")
        assert result["xmult"] == 3.0

    def test_chips_with_flavor_text(self):
        from balatro_bot.joker_effects import parse_effect_value
        result = parse_effect_value("+100 Chips -5 for each hand played")
        assert result["chips"] == 100.0

    def test_xmult_with_flavor_text(self):
        from balatro_bot.joker_effects import parse_effect_value
        result = parse_effect_value("X3 Mult 1 in 1000 chance this card is destroyed at end of round")
        assert result["xmult"] == 3.0

    def test_multi_value(self):
        from balatro_bot.joker_effects import parse_effect_value
        result = parse_effect_value("+20 Chips +4 Mult per scored Ace")
        assert result["chips"] == 20.0
        assert result["mult"] == 4.0

    def test_empty_string(self):
        from balatro_bot.joker_effects import parse_effect_value
        result = parse_effect_value("")
        assert result["chips"] is None
        assert result["mult"] is None
        assert result["xmult"] is None

    def test_no_match(self):
        from balatro_bot.joker_effects import parse_effect_value
        result = parse_effect_value("Creates a random Joker card")
        assert result["chips"] is None
        assert result["mult"] is None
        assert result["xmult"] is None

    def test_current_value_reads_current(self):
        from balatro_bot.joker_effects import current_value
        j = {"key": "j_green_joker", "value": {"current": {"mult": 12}}}
        assert current_value(j, "mult", fallback=5) == 12.0

    def test_current_value_fallback(self):
        from balatro_bot.joker_effects import current_value
        j = {"key": "j_green_joker", "value": {}}
        assert current_value(j, "mult", fallback=5) == 5

    def test_current_value_ignores_effect_text(self):
        from balatro_bot.joker_effects import current_value
        j = {"key": "j_green_joker", "value": {"effect": "+12 Mult"}}
        assert current_value(j, "mult", fallback=5) == 5

    def test_scoring_uses_current_value(self):
        from balatro_bot.joker_effects.live_values import enrich_state
        cards = [card("K", "H"), card("K", "D")]
        state = {"jokers": {"cards": [
            {"key": "j_green_joker", "set": "JOKER", "cost": {"sell": 3},
             "value": {"ability": {"mult": 12, "hand_add": 1, "discard_sub": 1}}},
        ]}}
        j_live = enrich_state(state)["jokers"]["cards"][0]
        j_no_value = {"key": "j_green_joker", "set": "JOKER", "cost": {"sell": 3}, "value": {}}
        _, _, total_live = score_hand("Pair", cards, jokers=[j_live])
        _, _, total_fallback = score_hand("Pair", cards, jokers=[j_no_value])
        # Green Joker adds current mult + hand_add(1): 12 + 1 = 13 mult
        assert total_live == 450  # 30 chips * (2 base + 12 current + 1 hand_add) = 450
        assert total_fallback == 90  # 30 chips * (2 base + 0 fallback + 1 hand_add) = 90


# ---------------------------------------------------------------------------
# Shortcut joker tests
# ---------------------------------------------------------------------------

class TestShortcut:
    def test_shortcut_straight_gaps_of_2(self):
        """2-4-6-8-T with gaps of 2 is a Straight with Shortcut."""
        cards = [card("2", "H"), card("4", "D"), card("6", "C"), card("8", "S"), card("T", "H")]
        assert classify_hand(cards, shortcut=True) == "Straight"
        assert classify_hand(cards, shortcut=False) == "High Card"

    def test_shortcut_straight_odd_gaps(self):
        """3-5-7-9-J with gaps of 2 is a Straight with Shortcut."""
        cards = [card("3", "H"), card("5", "D"), card("7", "C"), card("9", "S"), card("J", "H")]
        assert classify_hand(cards, shortcut=True) == "Straight"

    def test_shortcut_gap_of_3_fails(self):
        """2-4-7-9-J has a gap of 3 (4→7) — NOT a Straight even with Shortcut."""
        cards = [card("2", "H"), card("4", "D"), card("7", "C"), card("9", "S"), card("J", "H")]
        assert classify_hand(cards, shortcut=True) == "High Card"

    def test_shortcut_mixed_gaps(self):
        """3-4-6-8-T: gaps of 1,2,2,2 — valid Shortcut straight."""
        cards = [card("3", "H"), card("4", "D"), card("6", "C"), card("8", "S"), card("T", "H")]
        assert classify_hand(cards, shortcut=True) == "Straight"

    def test_shortcut_ace_low(self):
        """A-2-4 with more cards — ace-low Shortcut straight."""
        cards = [card("A", "H"), card("2", "D"), card("4", "C"), card("6", "S"), card("8", "H")]
        assert classify_hand(cards, shortcut=True) == "Straight"

    def test_shortcut_normal_straight_still_works(self):
        """Normal consecutive straight still works with Shortcut enabled."""
        cards = [card("5", "H"), card("6", "D"), card("7", "C"), card("8", "S"), card("9", "H")]
        assert classify_hand(cards, shortcut=True) == "Straight"

    def test_four_fingers_plus_shortcut(self):
        """4 cards with gaps of 2 — Straight with both Four Fingers + Shortcut."""
        cards = [card("2", "H"), card("4", "D"), card("6", "C"), card("8", "S")]
        assert classify_hand(cards, four_fingers=True, shortcut=True) == "Straight"
        assert classify_hand(cards, four_fingers=True, shortcut=False) == "High Card"


# ---------------------------------------------------------------------------
# Smeared joker tests
# ---------------------------------------------------------------------------

class TestSmeared:
    def test_smeared_hearts_diamonds_flush(self):
        """3H + 2D = Flush when Hearts and Diamonds are merged."""
        cards = [card("2", "H"), card("5", "H"), card("8", "D"), card("J", "D"), card("A", "H")]
        assert classify_hand(cards, smeared=True) == "Flush"
        assert classify_hand(cards, smeared=False) == "High Card"

    def test_smeared_clubs_spades_flush(self):
        """Mixed Clubs + Spades = Flush when merged."""
        cards = [card("3", "C"), card("7", "S"), card("T", "S"), card("Q", "C"), card("A", "C")]
        assert classify_hand(cards, smeared=True) == "Flush"
        assert classify_hand(cards, smeared=False) == "High Card"

    def test_smeared_no_cross_group(self):
        """Hearts + Clubs do NOT merge — different groups."""
        cards = [card("2", "H"), card("5", "H"), card("8", "C"), card("J", "C"), card("A", "H")]
        assert classify_hand(cards, smeared=True) == "High Card"

    def test_smeared_straight_flush(self):
        """Smeared can enable Straight Flush with merged suits."""
        cards = [card("5", "H"), card("6", "D"), card("7", "H"), card("8", "D"), card("9", "H")]
        assert classify_hand(cards, smeared=True) == "Straight Flush"
        assert classify_hand(cards, smeared=False) == "Straight"

    def test_four_fingers_plus_smeared(self):
        """4-card flush with merged suits."""
        cards = [card("3", "H"), card("7", "D"), card("T", "D"), card("K", "H")]
        assert classify_hand(cards, four_fingers=True, smeared=True) == "Flush"
        assert classify_hand(cards, four_fingers=True, smeared=False) == "High Card"

    def test_smeared_pure_suit_still_works(self):
        """All same suit still works with Smeared enabled."""
        cards = [card("2", "H"), card("5", "H"), card("8", "H"), card("J", "H"), card("A", "H")]
        assert classify_hand(cards, smeared=True) == "Flush"


# ---------------------------------------------------------------------------
# Hand search: the shortcuts must agree with the full search
# ---------------------------------------------------------------------------

def _search_deck() -> list:
    """A deck with every card feature hand search treats specially."""
    from balatro_bot.domain.models.card import Card, CardModifier, CardState, CardValue

    deck = [card(r, su) for r in "23456789TJQKA" for su in "HDCS"]
    deck += [card("K", "H", "STEEL"), card("9", "S", "GLASS"), card("5", "D", "BONUS"),
             card("Q", "C", "MULT"), card("7", "H", "LUCKY"), card("J", "S", "GOLD"),
             stone_card(), wild_card("8", "D"), wild_card("3", "C")]
    deck += [
        Card(key="H_4", label="4 of H", value=CardValue(rank="4", suit="H"),
             modifier=CardModifier(seal="RED"), state=CardState()),
        Card(key="S_A", label="A of S", value=CardValue(rank="A", suit="S"),
             modifier=CardModifier(edition="POLYCHROME"), state=CardState()),
        Card(key="C_T", label="T of C", value=CardValue(rank="T", suit="C", perma_bonus=15),
             modifier=CardModifier(edition="HOLO"), state=CardState()),
        Card(key="D_Q", label="Q of D", value=CardValue(rank="Q", suit="D"),
             modifier=CardModifier(enhancement="STEEL"), state=CardState(debuff=True)),
    ]
    return deck


_SEARCH_LINEUPS = [
    [],
    ["j_joker", "j_jolly"],
    ["j_photograph", "j_hanging_chad", "j_scary_face"],
    ["j_baron", "j_mime", "j_shoot_the_moon", "j_raised_fist"],
    ["j_bloodstone", "j_fibonacci", "j_hack"],
    ["j_dna", "j_triboulet", "j_sock_and_buskin"],
    ["j_splash", "j_blackboard"],
    ["j_four_fingers", "j_shortcut", "j_smeared", "j_crazy", "j_droll"],
    ["j_midas_mask", "j_vampire", "j_pareidolia"],
    ["j_blueprint", "j_duo", "j_brainstorm"],
]


def _summary(c):
    return (c.hand_name, c.card_indices, c.chips, c.mult, c.total,
            [id(x) for x in c.scoring_cards])


class TestSearchShortcuts:
    def _cases(self):
        import random
        rng = random.Random(20261114)
        deck = _search_deck()
        for keys in _SEARCH_LINEUPS:
            for _ in range(6):
                yield rng.sample(deck, 8), [joker(k) for k in keys]

    def test_best_hand_is_the_first_enumerated_hand(self):
        for hand, jokers in self._cases():
            for kwargs in ({}, {"required_hand": "Pair"}, {"excluded_hands": {"High Card", "Pair"}},
                           {"min_select": 5}, {"required_card_indices": {0, 3}},
                           {"hand_affinity": {"Flush": 2.0, "Two Pair": 1.0}}):
                every = enumerate_hands(hand, jokers=jokers, hands_left=1, **kwargs)
                best = best_hand(hand, jokers=jokers, hands_left=1, **kwargs)
                if not every:
                    assert best is None
                else:
                    assert _summary(best) == _summary(every[0])

    def test_required_hand_keeps_the_matching_hands_unchanged(self):
        for hand, jokers in self._cases():
            every = enumerate_hands(hand, jokers=jokers)
            pairs = enumerate_hands(hand, jokers=jokers, required_hand="Pair")
            assert [_summary(c) for c in pairs] == [_summary(c) for c in every if c.hand_name == "Pair"]

    def test_raw_dict_cards_score_like_typed_cards(self):
        from balatro_bot.domain.models.card import card_from_dict

        def raw(rank, suit, **modifier):
            return {"label": f"{rank} of {suit}", "set": "DEFAULT",
                    "value": {"rank": rank, "suit": suit}, "modifier": modifier, "state": []}

        hand = [raw("K", "H", enhancement="STEEL"), raw("K", "S"), raw("9", "S", enhancement="GLASS"),
                raw("9", "D", seal="RED"), raw("4", "C", edition="POLYCHROME"), raw("J", "H"),
                raw("A", "S", edition="HOLO"), raw("2", "D", enhancement="WILD")]
        typed = [card_from_dict(c) for c in hand]
        for keys in _SEARCH_LINEUPS:
            jokers = [joker(k) for k in keys]
            from_raw = enumerate_hands(hand, jokers=jokers)
            from_typed = enumerate_hands(typed, jokers=jokers)
            assert [_summary(c)[:5] for c in from_raw] == [_summary(c)[:5] for c in from_typed]

    def test_score_hand_agrees_with_search(self):
        """One-off score_hand and the search's reused scorer give the same numbers."""
        for hand, jokers in self._cases():
            for c in enumerate_hands(hand, jokers=jokers)[:25]:
                held = [x for i, x in enumerate(hand) if i not in c.card_indices]
                # Search orders the played cards; re-score in the same order it used.
                from balatro_bot.domain.scoring.play_order import sort_play_order
                order = sort_play_order(list(c.card_indices), hand, jokers) if jokers else c.card_indices
                played = [hand[i] for i in order]
                assert score_hand(c.hand_name, c.scoring_cards, jokers=jokers,
                                  played_cards=played, held_cards=held if jokers else None,
                                  ) == (c.chips, c.mult, c.total)

    def test_held_cards_score_left_to_right_in_a_nine_card_hand(self):
        """A held Queen's +13 Mult lands before or after a held Steel card's X1.5 by position."""
        hand = [card("2", "D"), card("Q", "H"), card("Q", "C"), card("3", "H"), card("5", "S"),
                card("7", "C"), card("8", "D"), card("9", "H"), card("K", "S", "STEEL")]
        jokers = [joker("j_shoot_the_moon")]
        for c in enumerate_hands(hand, jokers=jokers):
            held = [x for i, x in enumerate(hand) if i not in c.card_indices]
            assert score_hand(c.hand_name, c.scoring_cards, jokers=jokers,
                              played_cards=[hand[i] for i in c.card_indices], held_cards=held,
                              ) == (c.chips, c.mult, c.total)


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
