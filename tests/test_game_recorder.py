"""Game recorder: one short game of canned states, and the dashboard payload it makes."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from balatrobot import APIError

from balatro_bot.game_recorder import GameRecorder

HAND_LEVELS = {
    "High Card": {"chips": 5, "mult": 1, "level": 1},
    "Pair": {"chips": 10, "mult": 2, "level": 2},
    "Two Pair": {"chips": 20, "mult": 2, "level": 1},
    "Three of a Kind": {"chips": 30, "mult": 3, "level": 1},
    "Straight": {"chips": 30, "mult": 4, "level": 1},
    "Flush": {"chips": 35, "mult": 4, "level": 1},
}


def _card(rank, suit):
    return {"key": f"{suit}_{rank}", "set": "DEFAULT", "label": f"{rank} of {suit}",
            "value": {"rank": rank, "suit": suit}, "modifier": {}, "state": {}, "cost": {}}


def _jolly():
    return {"key": "j_jolly", "label": "Jolly Joker", "set": "JOKER",
            "cost": {"buy": 3, "sell": 1}, "value": {"effect": "+8 Mult if played hand contains a Pair"}}


HAND = [_card("K", "H"), _card("K", "D"), _card("3", "C"), _card("5", "S"), _card("7", "H")]
DECK = [_card(r, s) for r in ("A", "Q", "9") for s in ("H", "S")]


def _state(name, *, money=4, chips=0, jokers=(), blind="SELECT", **extra):
    return {
        "state": name, "seed": "TEST", "ante_num": 1, "round_num": 1, "money": money,
        "hands": HAND_LEVELS,
        "hand": {"cards": list(HAND) if name == "SELECTING_HAND" else []},
        "jokers": {"cards": list(jokers), "limit": 5},
        "consumables": {"cards": [], "limit": 2},
        "cards": {"cards": list(DECK), "count": len(DECK)},
        "round": {"chips": chips, "hands_left": 4, "discards_left": 3},
        "blinds": {"small": {"name": "Small Blind", "score": 300, "status": blind}},
        "used_vouchers": {}, "vouchers": {"cards": []}, "packs": {"cards": []},
        **extra,
    }


def _shop(money, jokers=(), shop_cards=None):
    cards = [_jolly()] if shop_cards is None else shop_cards
    return _state("SHOP", money=money, jokers=jokers, blind="UPCOMING",
                  shop={"cards": cards},
                  packs={"cards": [{"label": "Buffoon Pack", "cost": {"buy": 4}}]})


def _act(rec, before, method, params, after, *, action=None, decision=None, recording_at=None):
    rec.observe(before)
    rec.sending(before["state"], method, params, action or SimpleNamespace(reason="test"), decision, before)
    rec.acted(after, recording_at)
    return after


@pytest.fixture
def game() -> GameRecorder:
    """A small blind lost after one play and a discard, with a shop visit before it."""
    rec = GameRecorder()
    shop = _shop(10)
    after_buy = _act(rec, shop, "buy", {"card": 0}, _shop(7, jokers=[_jolly()], shop_cards=[]))
    after_reroll = _act(rec, after_buy, "reroll", None, _shop(2, jokers=[_jolly()]))
    after_sell = _act(rec, after_reroll, "sell", {"joker": 0}, _shop(3))
    pack = _state("BUFFOON_PACK", money=3, blind="UPCOMING", pack={"cards": [_jolly()]})
    rec.observe(pack)
    rec.sending("BUFFOON_PACK", "pack", {"card": 0}, SimpleNamespace(reason="pick"),
                {"kind": "pack", "chosen_idx": 0, "policy": "explore"}, pack)
    rec.acted(_state("BLIND_SELECT", money=3, jokers=[_jolly()]), recording_at=6)
    del after_sell

    blind = _state("BLIND_SELECT", money=3, jokers=[_jolly()])
    hand = _act(rec, blind, "select", None, _state("SELECTING_HAND", money=3, jokers=[_jolly()], blind="CURRENT"))
    played = _act(rec, hand, "play", {"cards": [0, 1]},
                  _state("SELECTING_HAND", money=3, chips=64, jokers=[_jolly()], blind="CURRENT"),
                  action=SimpleNamespace(reason="pair", hand_name="Pair", total=64))
    _act(rec, played, "discard", {"cards": [2]},
         _state("SELECTING_HAND", money=3, chips=64, jokers=[_jolly()], blind="CURRENT"))
    rec.finish(_state("GAME_OVER", money=3, chips=64, jokers=[_jolly()], blind="CURRENT"))
    return rec


def _payload(rec, replayable=False):
    final = _state("GAME_OVER", money=3, chips=64, jokers=[_jolly()], blind="CURRENT")
    return rec.payload(final, replayable=replayable, port=12000, deck="BLUE", stake="WHITE", log_text="log")


def test_every_action_is_logged_in_order(game):
    p = _payload(game)
    assert [a["action_type"] for a in p["actions_log"]] == ["buy", "reroll", "sell", "pack", "select", "play", "discard"]
    assert [a["seq"] for a in p["actions_log"]] == list(range(1, 8))
    assert p["actions"] == 7 and p["total_hands"] == 1 and p["total_discards"] == 1


def test_shop_events_carry_cost_and_money(game):
    events = [(e["event_type"], e["item_name"], e["item_type"], e["cost"]) for e in _payload(game)["shop_events"]
              if e["event_type"] != "offer"]
    assert events == [
        ("buy", "Jolly Joker", "joker", 3),
        ("reroll", None, None, 5),
        ("sell", "Jolly Joker", "joker", None),
        ("pick", "Jolly Joker", "pack", None),
    ]
    p = _payload(game)
    assert p["rerolls"] == 1 and p["pack_picks"] == 1 and p["packs_opened"] == 1


def test_each_shop_roster_and_pack_is_offered_once(game):
    p = _payload(game)
    assert [o["shop_visit_seq"] for o in p["shop_offers"]] == [1, 1, 1]
    assert [o["pack_seq"] for o in p["pack_offers"]] == [1]


def test_a_sold_joker_is_marked_and_a_pack_joker_kept(game):
    jokers = _payload(game)["jokers"]
    assert [(j["joker_name"], j["source"], j["sell_ante"], j["in_final_roster"]) for j in jokers] == [
        ("Jolly Joker", "shop", 1, True),
        ("Jolly Joker", "pack", None, True),
    ]


def test_a_played_hand_is_scored(game):
    p = _payload(game)
    assert p["hand_types"] == {"Pair": 1}
    [score] = p["hand_scores"]
    assert (score["hand_type"], score["total_score"], score["blind_name"]) == ("Pair", 64, "Small Blind")


def test_the_lost_blind_is_the_death(game):
    p = _payload(game)
    assert p["win"] is False
    [round_] = p["rounds"]
    assert (round_["blind_name"], round_["scored"], round_["needed"], round_["won"]) == ("Small Blind", 64, 300, False)
    assert p["death_info"]["deficit"] == 236
    assert p["blind_decisions"][0]["action"] == "select"


def test_an_explored_choice_is_kept_for_the_replay(game):
    assert game.explored_actions == [6]
    [decision] = _payload(game)["decisions"]
    assert (decision["kind"], decision["policy"], decision["seq"]) == ("pack", "explore", 4)


def test_a_replayable_game_sends_only_its_summary(game):
    full, summary = _payload(game), _payload(game, replayable=True)
    for key in ("actions_log", "hand_scores", "shop_events", "shop_offers", "pack_offers", "decisions"):
        assert full[key] and summary[key] == []
    assert full["log_text"] == "log" and summary["log_text"] is None
    for key in ("rounds", "jokers", "ante_snapshots", "blind_decisions", "death_info", "consumables"):
        assert summary[key] == full[key]


def test_live_stats_wait_for_a_finished_round():
    rec = GameRecorder()
    assert rec.live_stats() is None


def test_live_stats_average_the_rounds(game):
    stats = game.live_stats()
    assert stats.avg_plays_per_round == 1 and stats.avg_discards_per_round == 1
    assert stats.avg_sells_per_ante == 1


def test_a_refused_choice_is_recorded_with_its_error():
    rec = GameRecorder()
    shop = _shop(10)
    rec.observe(shop)
    rec.sending("SHOP", "buy", {"card": 0}, SimpleNamespace(reason="buy"), {"kind": "shop", "chosen_idx": 0}, shop)
    rec.refused(APIError("NOT_ALLOWED", "joker slots are full", 400))
    p = _payload(rec)
    assert p["actions"] == 0 and p["actions_log"] == []
    assert p["decisions"][0]["error"] == "NOT_ALLOWED" and p["decisions"][0]["money_after"] is None


def test_a_timed_out_play_counts_as_played():
    rec = GameRecorder()
    hand = _state("SELECTING_HAND", blind="CURRENT")
    rec.observe(hand)
    rec.sending("SELECTING_HAND", "play", {"cards": [0, 1]}, SimpleNamespace(reason="pair", hand_name="Pair"), None, hand)
    rec.timed_out()
    assert _payload(rec)["total_hands"] == 1


def test_victory_needs_ante_nine():
    rec = GameRecorder()
    rec.observe(_state("SHOP", blind="UPCOMING", won=True))
    assert not rec.won
    rec.observe({**_state("SHOP", blind="UPCOMING"), "ante_num": 9})
    assert rec.won
