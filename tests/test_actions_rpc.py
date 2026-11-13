"""RPC parameter names must match the balatrobot mod's endpoint schemas."""

from balatro_bot.actions import PackAction, UseConsumable


def test_pack_pick_sends_hand_targets_as_targets():
    assert PackAction(card_index=1, targets=[6]).to_rpc() == ("pack", {"card": 1, "targets": [6]})


def test_pack_pick_without_targets():
    assert PackAction(card_index=0).to_rpc() == ("pack", {"card": 0})


def test_pack_skip():
    assert PackAction().to_rpc() == ("pack", {"skip": True})


def test_use_consumable_sends_hand_targets_as_cards():
    assert UseConsumable(index=0, target_cards=[2, 3]).to_rpc() == ("use", {"consumable": 0, "cards": [2, 3]})
