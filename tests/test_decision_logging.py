"""Shop and pack decision records: what the bot weighed and chose."""

from __future__ import annotations

from types import SimpleNamespace

from balatro_bot.bot_logging import build_decision_state, deck_composition, record_decision
from balatro_bot.domain.policy.pack_policy import choose_from_buffoon_pack, choose_from_planet_pack
from balatro_bot.domain.policy.shop_evaluator import ShopEvaluator
from balatro_bot.engine import RuleEngine
from balatro_bot.rules.packs import PickFromPlanetPack, PickFromStandardPack
from balatro_bot.strategy import compute_strategy
from tests.conftest import joker


HAND_LEVELS = {
    "High Card": {"chips": 5, "mult": 1, "level": 1},
    "Pair": {"chips": 10, "mult": 2, "level": 2},
    "Two Pair": {"chips": 20, "mult": 2, "level": 1},
    "Three of a Kind": {"chips": 30, "mult": 3, "level": 1},
    "Straight": {"chips": 30, "mult": 4, "level": 1},
    "Flush": {"chips": 35, "mult": 4, "level": 1},
    "Full House": {"chips": 40, "mult": 4, "level": 1},
    "Four of a Kind": {"chips": 60, "mult": 7, "level": 1},
}


def _playing_card(rank: str, suit: str, enhancement: str | None = None) -> dict:
    return {
        "key": f"{suit}_{rank}", "set": "DEFAULT", "label": f"{rank} of {suit}",
        "value": {"rank": rank, "suit": suit},
        "modifier": {"enhancement": enhancement} if enhancement else {},
        "state": {}, "cost": {},
    }


def _deck() -> list[dict]:
    return [_playing_card(r, s) for r in ("A", "K", "Q", "J", "T", "9", "8") for s in ("H", "S", "D", "C")]


def _shop_state(money: int = 12) -> dict:
    jolly = joker("j_jolly", "Jolly Joker")
    jolly.update({"set": "JOKER", "cost": {"buy": 3}, "value": {"effect": "+8 Mult if played hand contains a Pair"}})
    return {
        "state": "SHOP",
        "money": money,
        "ante_num": 1,
        "round_num": 1,
        "jokers": {"cards": [], "count": 0, "limit": 5},
        "consumables": {"cards": [], "count": 0, "limit": 2},
        "hands": HAND_LEVELS,
        "shop": {"cards": [jolly]},
        "vouchers": {"cards": []},
        "packs": {"cards": []},
        "used_vouchers": {},
        "cards": {"cards": _deck()},
        "hand": {"cards": []},
        "blinds": {"small": {"name": "Small Blind", "status": "UPCOMING"}},
    }


class TestShopDecision:
    def test_chosen_plan_is_first_and_highest(self):
        ev = ShopEvaluator()
        action = ev.evaluate(_shop_state())
        rec = ev.last_decision
        assert rec["kind"] == "shop"
        assert rec["continuation"] is False
        assert rec["chosen_idx"] == 0
        values = [c["value"] for c in rec["candidates"]]
        assert values[0] == max(values)
        assert rec["candidates"][0]["steps"][0] == list(action.to_rpc())

    def test_candidates_capped_and_budget_present(self):
        ev = ShopEvaluator()
        ev.evaluate(_shop_state())
        rec = ev.last_decision
        assert 1 <= len(rec["candidates"]) <= 8
        assert rec["n_candidates"] >= len(rec["candidates"])
        assert rec["budget"]["phase"] in ("BUILD", "FLEX", "SPEND")

    def test_engine_exposes_the_rule_that_fired(self):
        engine = RuleEngine()
        action = engine.decide(_shop_state())
        assert action is not None
        rec = getattr(engine.last_rule, "last_decision", None)
        assert rec is not None and rec["kind"] == "shop"


class TestPackOptions:
    def test_planet_records_every_card(self):
        cards = [{"key": "c_mercury", "label": "Mercury"}, {"key": "c_jupiter", "label": "Jupiter"}]
        strat = compute_strategy([], HAND_LEVELS)
        options: list[dict] = []
        idx, score, _ = choose_from_planet_pack(cards, strat, HAND_LEVELS, set(), options_out=options)
        assert [o["idx"] for o in options] == [0, 1]
        assert options[idx]["score"] == round(score, 3)

    def test_buffoon_records_exclusions(self):
        owned = [joker(f"j_x{i}", f"X{i}") for i in range(5)]
        pack = [joker("j_jolly", "Jolly Joker")]
        pack[0]["set"] = "JOKER"
        strat = compute_strategy([], HAND_LEVELS)
        options: list[dict] = []
        choose_from_buffoon_pack(pack, owned, HAND_LEVELS, 1, 5, strat, set(), options_out=options)
        assert options == [{"idx": 0, "key": "j_jolly", "excluded": "slots_full"}]

    def test_planet_rule_sets_last_decision(self):
        rule = PickFromPlanetPack()
        state = {
            "state": "PLANET_PACK", "hands": HAND_LEVELS, "jokers": {"cards": []},
            "pack": {"cards": [{"key": "c_mercury", "label": "Mercury"}, {"key": "c_venus", "label": "Venus"}]},
        }
        action = rule.evaluate(state)
        rec = rule.last_decision
        assert rec["kind"] == "pack" and rec["rule"] == "pick_from_planet_pack"
        assert rec["chosen_idx"] == action.card_index
        assert len(rec["options"]) == 2

    def test_standard_skip_records_none(self):
        rule = PickFromStandardPack()
        state = {
            "state": "STANDARD_PACK", "hands": HAND_LEVELS, "jokers": {"cards": []},
            "pack": {"cards": [_playing_card("2", "C")]},
        }
        action = rule.evaluate(state)
        assert action.card_index is None
        assert rule.last_decision["chosen_idx"] is None
        assert rule.last_decision["threshold"] == PickFromStandardPack._PICK_THRESHOLD


class TestDecisionRecord:
    def test_state_snapshot_shape(self):
        state = _shop_state()
        state["jokers"]["cards"] = [dict(joker("j_jolly", "Jolly Joker"), value={"effect": "+8 Mult"})]
        gs = SimpleNamespace(shop_visit_seq=2, pack_seq=0)
        snap = build_decision_state(gs, state)
        assert snap["money"] == 12 and snap["ante"] == 1 and snap["shop_visit_seq"] == 2
        assert snap["jokers"][0]["key"] == "j_jolly"
        assert snap["hand_levels"] == {"Pair": 2}
        assert snap["deck"]["total"] == 28
        assert snap["shop"][0]["key"] == "j_jolly"
        assert snap["pack"] is None

    def test_record_flattens_decision(self):
        gs = SimpleNamespace(actions_taken=7, decisions=[])
        decision = {"kind": "pack", "rule": "r", "options": [], "chosen_idx": None, "reason": "skip"}
        record_decision(gs, decision, {"money": 3}, "PLANET_PACK", "pack", {"skip": True}, 3, 3)
        rec = gs.decisions[0]
        assert rec["seq"] == 7 and rec["kind"] == "pack" and rec["policy"] == "greedy"
        assert rec["chosen_idx"] is None and rec["error"] is None
        assert rec["detail"] == {"rule": "r", "options": [], "reason": "skip"}

    def test_deck_composition_counts(self):
        comp = deck_composition([_playing_card("A", "H", "STEEL"), _playing_card("A", "S")])
        assert comp["total"] == 2
        assert comp["ranks"] == {"A": 2}
        assert comp["enhancements"] == {"STEEL": 1, "NONE": 1}
