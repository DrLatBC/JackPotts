"""Death turns the left selected card into the right one, so the bot has to put the
card it wants two of to the right of the card it gives up before using Death."""

from __future__ import annotations

from balatro_bot.actions import PackAction, RearrangeHand
from balatro_bot.domain.policy.consumable_policy import _score_targeting_use
from balatro_bot.domain.policy.targeting import clone_reorder, find_clone_targets
from balatro_bot.rules.packs import PickFromTarotPack
from balatro_bot.strategy import compute_strategy

HAND_LEVELS = {
    "High Card": {"chips": 5, "mult": 1, "level": 1},
    "Pair": {"chips": 10, "mult": 2, "level": 1},
}
# Ace is the card worth copying, the 2 the one worth giving up.
SORTED_HAND = ["A", "K", "9", "7", "5", "2"]


def _hand(ranks):
    """Hand cards as the game state carries them."""
    return [
        {"key": f"S_{r}", "label": f"{r} of Spades", "set": "DEFAULT",
         "value": {"rank": r, "suit": "S"}, "modifier": [], "state": []}
        for r in ranks
    ]


def _ranks(hand):
    return [c["value"]["rank"] for c in hand]


def _reordered(hand, order):
    return [hand[i] for i in order]


def _death_use(hand):
    """The consumable policy's answer for Death (consumable 0) with this hand."""
    strat = compute_strategy([], HAND_LEVELS)
    _, args = _score_targeting_use(
        0, "c_death", "Death", 2, "clone", None,
        {}, strat, hand, [], HAND_LEVELS, None, 0,
        300, False, False, 5, 3, 4, 5, 1,
    )
    return args


def _pack_state(hand):
    return {
        "state": "SMODS_BOOSTER_OPENED",
        "pack": {"cards": [{"key": "c_death", "label": "Death", "set": "TAROT"}]},
        "hand": {"cards": hand},
        "jokers": {"cards": [], "count": 0, "limit": 5},
        "hands": HAND_LEVELS,
        "ante_num": 1,
        "money": 5,
        "consumables": {"cards": [], "count": 0, "limit": 2},
    }


class TestCloneReorder:
    def test_best_already_right_of_worst_needs_no_reorder(self):
        assert clone_reorder(best_idx=5, worst_idx=1, hand_size=6) is None

    def test_best_moves_to_just_right_of_worst(self):
        assert clone_reorder(best_idx=0, worst_idx=5, hand_size=6) == [1, 2, 3, 4, 5, 0]
        assert clone_reorder(best_idx=1, worst_idx=3, hand_size=6) == [0, 2, 3, 1, 4, 5]

    def test_the_reordered_hand_needs_no_second_reorder(self):
        hand = _hand(SORTED_HAND)
        best, worst = find_clone_targets(hand)
        hand = _reordered(hand, clone_reorder(best, worst, len(hand)))
        best, worst = find_clone_targets(hand)
        assert _ranks(hand)[best] == "A" and _ranks(hand)[worst] == "2"
        assert clone_reorder(best, worst, len(hand)) is None


class TestDeathFromTheConsumableArea:
    def test_best_card_left_of_worst_is_moved_right_first(self):
        hand = _hand(SORTED_HAND)
        kind, order, reason = _death_use(hand)
        assert kind == "rearrange"
        assert _ranks(_reordered(hand, order)) == ["K", "9", "7", "5", "2", "A"]
        assert "right of" in reason

    def test_death_is_used_once_the_best_card_is_on_the_right(self):
        hand = _hand(["K", "9", "7", "5", "2", "A"])
        kind, idx, targets, _ = _death_use(hand)
        assert (kind, idx) == ("use_target", 0)
        best, worst = targets
        assert _ranks(hand)[best] == "A" and _ranks(hand)[worst] == "2"
        assert best > worst

    def test_reorder_then_use_keeps_the_best_card(self):
        hand = _hand(SORTED_HAND)
        _, order, _ = _death_use(hand)
        hand = _reordered(hand, order)
        kind, _, (best, worst), _ = _death_use(hand)
        assert kind == "use_target"
        assert _ranks(hand)[max(best, worst)] == "A"  # the right-hand card is the one kept


class TestDeathFromAPack:
    def test_best_card_left_of_worst_is_moved_right_before_the_pick(self):
        hand = _hand(SORTED_HAND)
        rule = PickFromTarotPack()
        action = rule.evaluate(_pack_state(hand))
        assert isinstance(action, RearrangeHand)
        assert action.to_rpc() == ("rearrange", {"hand": [1, 2, 3, 4, 5, 0]})
        assert rule.last_decision is None  # nothing is picked on this tick

        action = rule.evaluate(_pack_state(_reordered(hand, action.order)))
        assert isinstance(action, PackAction)
        method, params = action.to_rpc()
        assert method == "pack" and params["card"] == 0
        assert list(params["targets"]) == [5, 4]  # the Ace, now right of the 2
        assert rule.last_decision is not None and rule.last_decision["chosen_idx"] == 0

    def test_no_reorder_when_the_best_card_is_already_on_the_right(self):
        action = PickFromTarotPack().evaluate(_pack_state(_hand(["2", "5", "7", "9", "K", "A"])))
        assert isinstance(action, PackAction)
        method, params = action.to_rpc()
        assert (method, params["card"], list(params["targets"])) == ("pack", 0, [5, 0])
