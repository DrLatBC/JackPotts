"""Dispatcher: plays in score order, carries the disabled-boss flag, recovers from refusals."""

from __future__ import annotations

import httpx
import pytest
from balatrobot import APIError

from balatro_bot.dispatcher import Dispatcher
from balatro_bot.engine import RuleEngine


def _state(name="SELECTING_HAND", hand=8, **extra):
    return {"state": name, "hand": {"cards": [{} for _ in range(hand)]}, **extra}


class FakeClient:
    """Answers each call from a script of results; a result that is an exception is raised."""

    def __init__(self, *results, recording=None):
        self.results = list(results)
        self.calls: list = []
        self.recording = recording

    def call(self, method, params=None):
        self.calls.append((method, params))
        if self.recording is not None and method not in ("gamestate", "highlight"):
            self.recording.append([method, params])
        result = self.results.pop(0) if self.results else _state()
        if isinstance(result, BaseException):
            raise result
        return result


def _refusal(name="BAD_REQUEST", message="no"):
    return APIError(name, message, 400)


def _dispatcher(client, engine=None):
    return Dispatcher(client, engine or RuleEngine(rules={"NONE": []}), poll_interval=0)


def test_a_play_in_hand_order_is_sent_as_is():
    client = FakeClient()
    params = {"cards": [0, 2, 4]}
    state = _state()
    assert _dispatcher(client).prepare("play", params, state) is state
    assert client.calls == [] and params == {"cards": [0, 2, 4]}


def test_a_play_out_of_order_rearranges_first():
    refreshed = _state(money=7)
    client = FakeClient({}, refreshed)
    params = {"cards": [4, 1]}
    state = _dispatcher(client).prepare("play", params, _state(hand=6))
    assert client.calls == [("rearrange", {"hand": [0, 4, 2, 3, 1, 5]}), ("gamestate", None)]
    assert params == {"cards": [1, 4]}
    assert state is refreshed


def test_a_failed_rearrange_plays_the_original_order():
    client = FakeClient(_refusal())
    params = {"cards": [4, 1]}
    before = _state(hand=6)
    assert _dispatcher(client).prepare("play", params, before) is before
    assert params == {"cards": [4, 1]}


def test_the_disabled_boss_flag_passes_to_every_new_state():
    client = FakeClient({}, _state(), _state(), _state())
    d = _dispatcher(client)
    flagged = _state(_boss_disabled=True)
    assert d.prepare("play", {"cards": [3, 0]}, flagged)["_boss_disabled"]
    assert d.poll(flagged)["_boss_disabled"]
    assert d.send("SELECTING_HAND", "discard", {"cards": [0]}, flagged).state["_boss_disabled"]
    assert "_boss_disabled" not in d.poll(_state())


def test_an_action_reports_its_place_in_the_recording():
    after = _state(money=3)
    client = FakeClient(after, recording=[["start", {}]])
    sent = _dispatcher(client).send("SHOP", "reroll", None, _state("SHOP"))
    assert (sent.outcome, sent.state, sent.recording_at) == ("acted", after, 1)


def test_a_refusal_is_reported_and_the_game_polled_again():
    polled = _state()
    client = FakeClient(_refusal(), polled)
    sent = _dispatcher(client).send("SELECTING_HAND", "discard", {"cards": [0]}, _state())
    assert sent.outcome == "refused" and sent.error.name == "BAD_REQUEST"
    assert sent.state is polled
    assert client.calls[-1] == ("gamestate", None)


def test_a_timeout_polls_the_game_again():
    polled = _state("GAME_OVER")
    client = FakeClient(httpx.ReadTimeout("slow"), polled)
    sent = _dispatcher(client).send("SELECTING_HAND", "play", {"cards": [0]}, _state())
    assert (sent.outcome, sent.state) == ("timed_out", polled)


def test_a_second_timeout_gives_up():
    client = FakeClient(httpx.ReadTimeout("slow"), httpx.ReadTimeout("slower"))
    with pytest.raises(httpx.TimeoutException):
        _dispatcher(client).send("SELECTING_HAND", "play", {"cards": [0]}, _state())


def test_a_wedged_pack_pick_is_skipped_on_the_third_refusal():
    wedged = _refusal("NOT_ALLOWED", "already in progress")
    skipped = _state("SHOP")
    client = FakeClient(wedged, _state("BUFFOON_PACK"), wedged, _state("BUFFOON_PACK"), wedged, skipped)
    d = _dispatcher(client)
    for _ in range(2):
        assert d.send("BUFFOON_PACK", "pack", {"card": 0}, _state("BUFFOON_PACK")).outcome == "refused"
    sent = d.send("BUFFOON_PACK", "pack", {"card": 0}, _state("BUFFOON_PACK"))
    assert client.calls[-1] == ("pack", {"skip": True})
    assert sent.state is skipped and d.consecutive_errors == 0


def test_a_pack_that_cannot_be_skipped_twice_ends_the_game():
    wedged = _refusal("NOT_ALLOWED", "already in progress")
    d = _dispatcher(FakeClient())
    d.consecutive_errors = 2
    d.client = FakeClient(wedged, wedged, _state("BUFFOON_PACK"))
    d.send("BUFFOON_PACK", "pack", {"card": 0}, _state("BUFFOON_PACK"))
    d.client = FakeClient(wedged, wedged)
    with pytest.raises(RuntimeError, match="pack state wedged"):
        d.send("BUFFOON_PACK", "pack", {"card": 0}, _state("BUFFOON_PACK"))


def test_five_refusals_in_a_row_force_a_pack_skip():
    client = FakeClient()
    d = _dispatcher(client)
    d.consecutive_errors = 4
    client.results = [_refusal(), {}, _state("SHOP")]
    sent = d.send("TAROT_PACK", "pack", {"card": 1}, _state("TAROT_PACK"))
    assert client.calls[-2:] == [("pack", {"skip": True}), ("gamestate", None)]
    assert sent.state["state"] == "SHOP" and d.consecutive_errors == 0


def test_an_action_clears_the_error_count():
    client = FakeClient(_refusal(), _state(), _state())
    d = _dispatcher(client)
    d.send("SHOP", "reroll", None, _state("SHOP"))
    assert d.consecutive_errors == 1
    d.send("SHOP", "reroll", None, _state("SHOP"))
    assert d.consecutive_errors == 0


def test_a_refused_consumable_is_blocked_for_the_round():
    class UseRule:
        def __init__(self):
            self._last_used_consumable = 2
            self._blocked_consumables: set = set()

    rule = UseRule()
    engine = RuleEngine(rules={"SHOP": [rule]})
    client = FakeClient(_refusal("NOT_ALLOWED", "cannot use"), _state("SHOP"))
    _dispatcher(client, engine).send("SHOP", "use", {"consumable": 2}, _state("SHOP"))
    assert rule._blocked_consumables == {2} and rule._last_used_consumable is None


def test_full_joker_slots_stop_joker_buys_this_visit():
    class Evaluator:
        slots_full = False

    class ShopRule:
        _evaluator = Evaluator()

    rule = ShopRule()
    engine = RuleEngine(rules={"SHOP": [rule]})
    client = FakeClient(_refusal("NOT_ALLOWED", "joker slots are full"), _state("SHOP"))
    _dispatcher(client, engine).send("SHOP", "buy", {"card": 0}, _state("SHOP"))
    assert rule._evaluator.slots_full
