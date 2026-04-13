from __future__ import annotations

from balatro_bot.actions import BuyCard
from balatro_bot.domain.policy.shop import choose_buy_consumable_in_shop
from tests.conftest import joker


def _shop_joker(key: str, label: str, buy: int, effect: str = "") -> dict:
    card = joker(key, label)
    card["cost"]["buy"] = buy
    if effect:
        card["value"] = {"effect": effect}
    return card


def test_choose_buy_consumable_in_shop_picks_best_affordable_value() -> None:
    state = {
        "money": 6,
        "ante_num": 2,
        "jokers": {"cards": []},
        "hands": {},
        "consumables": {"count": 0, "limit": 2, "cards": []},
        "shop": {
            "cards": [
                {"key": "c_high_priestess", "label": "The High Priestess", "set": "TAROT", "cost": {"buy": 3}},
                {"key": "c_world", "label": "The World", "set": "TAROT", "cost": {"buy": 4}},
            ],
        },
    }

    action = choose_buy_consumable_in_shop(state)

    assert action == BuyCard(0, reason="buy consumable: The High Priestess for $3 (value=5.0, $6->$3)")
