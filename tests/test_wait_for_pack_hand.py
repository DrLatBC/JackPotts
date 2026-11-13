"""Pack picks wait for the hand when a targeting card is on offer."""

from balatro_bot.actions import PackAction, Wait
from balatro_bot.engine import RuleEngine
from balatro_bot.rules import PickBestFromPack, WaitForPackHand


def _state(keys, hand_cards=()):
    return {
        "state": "SMODS_BOOSTER_OPENED",
        "pack": {"cards": [{"key": k, "label": k} for k in keys]},
        "hand": {"cards": list(hand_cards)},
    }


def test_waits_for_hand_when_targeting_tarot_offered():
    assert isinstance(WaitForPackHand().evaluate(_state(["c_devil", "c_hermit"])), Wait)


def test_waits_for_hand_when_targeting_spectral_offered():
    assert isinstance(WaitForPackHand().evaluate(_state(["c_talisman"])), Wait)


def test_passes_once_hand_is_dealt():
    assert WaitForPackHand().evaluate(_state(["c_devil"], [{"id": 1}])) is None


def test_passes_when_no_card_needs_targets():
    assert WaitForPackHand().evaluate(_state(["c_hermit", "c_temperance"])) is None


def test_gives_up_after_max_waits():
    rule = WaitForPackHand()
    state = _state(["c_devil"])
    for _ in range(WaitForPackHand.MAX_WAITS):
        assert isinstance(rule.evaluate(state), Wait)
    assert rule.evaluate(state) is None


def test_engine_stops_the_chain_and_polls_on_wait():
    engine = RuleEngine({"SMODS_BOOSTER_OPENED": [WaitForPackHand(), PickBestFromPack()]})
    assert engine.decide(_state(["c_devil"])) is None
    assert engine.last_rule is None


def test_engine_falls_through_once_hand_is_dealt():
    engine = RuleEngine({"SMODS_BOOSTER_OPENED": [WaitForPackHand(), PickBestFromPack()]})
    action = engine.decide(_state(["c_hermit"], [{"id": 1}]))
    assert isinstance(action, PackAction)
