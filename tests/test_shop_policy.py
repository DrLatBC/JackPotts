from __future__ import annotations

from balatro_bot.actions import BuyCard, BuyPack, BuyVoucher, NextRound, Reroll, SellConsumable, SellJoker
from balatro_bot.domain.policy.shop import (
    choose_buy_consumable_in_shop,
    choose_buy_joker_in_shop,
    choose_buy_pack_in_shop,
    choose_buy_voucher_in_shop,
    choose_feed_campfire,
    choose_leave_shop,
    choose_reroll_shop,
    choose_sell_diet_cola,
    choose_sell_weak_joker,
)
from balatro_bot.rules.shop import (
    BuyConsumablesInShop,
    BuyJokersInShop,
    BuyPacksInShop,
    BuyVouchersInShop,
    FeedCampfire,
    LeaveShop,
    RerollShop,
    SellDietCola,
    SellWeakJoker,
)
from tests.conftest import joker


def _shop_joker(key: str, label: str, buy: int, effect: str = "") -> dict:
    card = joker(key, label)
    card["cost"]["buy"] = buy
    if effect:
        card["value"] = {"effect": effect}
    return card


def test_choose_sell_weak_joker_sells_decayed_popcorn() -> None:
    state = {
        "jokers": {
            "count": 3,
            "limit": 5,
            "cards": [
                _shop_joker("j_popcorn", "Popcorn", 0, "+4 Mult"),
                joker("j_joker", "Joker"),
                joker("j_duo", "Duo"),
            ],
        },
    }

    action = choose_sell_weak_joker(state)

    assert action == SellJoker(0, reason="sell decayed Popcorn (+4.0 Mult, about to disappear)")


def test_choose_sell_diet_cola_skips_when_force_buy_joker_exists() -> None:
    state = {
        "jokers": {"cards": [joker("j_diet_cola", "Diet Cola")]},
        "shop": {"cards": [_shop_joker("j_duo", "Duo", 4)]},
    }

    action = choose_sell_diet_cola(state)

    assert action is None


def test_sell_diet_cola_rule_delegates_to_policy() -> None:
    state = {
        "jokers": {"cards": [joker("j_diet_cola", "Diet Cola")]},
        "shop": {"cards": []},
    }

    action = SellDietCola().evaluate(state)

    assert action == SellJoker(0, reason="Diet Cola: sell for free shop reroll")


def test_sell_weak_joker_rule_delegates_to_policy() -> None:
    state = {
        "jokers": {
            "count": 2,
            "limit": 5,
            "cards": [
                _shop_joker("j_ramen", "Ramen", 0, "X0.8 Mult"),
                joker("j_joker", "Joker"),
            ],
        },
    }

    action = SellWeakJoker().evaluate(state)

    assert action == SellJoker(0, reason="sell decayed Ramen (X0.80, reducing scores)")


def test_choose_feed_campfire_sells_off_strategy_planet() -> None:
    state = {
        "jokers": {"cards": [joker("j_campfire", "Campfire")]},
        "hands": {"Pair": {"level": 1, "chips": 10, "mult": 10}},
        "consumables": {
            "cards": [
                {"key": "c_jupiter", "label": "Jupiter"},
            ],
        },
    }

    action = choose_feed_campfire(state)

    assert action == SellConsumable(
        0,
        reason="Campfire: sell Jupiter (+X0.25 Mult, Flush has no affinity)",
    )


def test_feed_campfire_rule_delegates_to_policy() -> None:
    state = {
        "jokers": {"cards": [joker("j_campfire", "Campfire")]},
        "hands": {},
        "consumables": {
            "cards": [
                {"key": "c_foo", "label": "Mystery"},
            ],
        },
    }

    action = FeedCampfire().evaluate(state)

    assert action == SellConsumable(0, reason="Campfire: sell Mystery (+X0.25 Mult)")


def test_choose_buy_joker_in_shop_force_buys_first_joker() -> None:
    state = {
        "money": 4,
        "ante_num": 2,
        "hands": {"Pair": {"level": 1, "chips": 10, "mult": 10}},
        "jokers": {"count": 0, "limit": 5, "cards": []},
        "shop": {
            "cards": [
                _shop_joker(
                    "j_duo",
                    "Duo",
                    4,
                    "X2 Mult if played hand contains a Pair",
                ),
            ],
        },
    }

    action = choose_buy_joker_in_shop(state)

    assert action == BuyCard(0, reason="buy joker: Duo for $4 (value=10.0, $4->$0)")


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


def test_buy_jokers_rule_delegates_to_policy() -> None:
    state = {
        "money": 3,
        "ante_num": 2,
        "hands": {"Pair": {"level": 1, "chips": 10, "mult": 10}},
        "jokers": {"count": 0, "limit": 5, "cards": []},
        "shop": {
            "cards": [
                _shop_joker("j_popcorn", "Popcorn", 3, "+20 Mult"),
            ],
        },
    }

    action = BuyJokersInShop().evaluate(state)

    assert action == BuyCard(0, reason="buy joker: Popcorn for $3 (value=10.0, $3->$0)")


def test_buy_consumables_rule_delegates_to_policy() -> None:
    state = {
        "money": 6,
        "ante_num": 2,
        "jokers": {"cards": []},
        "hands": {},
        "consumables": {"count": 0, "limit": 2, "cards": []},
        "shop": {
            "cards": [
                {"key": "c_high_priestess", "label": "The High Priestess", "set": "TAROT", "cost": {"buy": 3}},
            ],
        },
    }

    action = BuyConsumablesInShop().evaluate(state)

    assert action == BuyCard(0, reason="buy consumable: The High Priestess for $3 (value=5.0, $6->$3)")


def test_choose_buy_voucher_in_shop_picks_highest_priority_affordable() -> None:
    state = {
        "money": 40,
        "vouchers": {
            "cards": [
                {"key": "v_wasteful", "label": "Wasteful", "cost": {"buy": 10}},
                {"key": "v_grabber", "label": "Grabber", "cost": {"buy": 10}},
            ],
        },
    }

    action = choose_buy_voucher_in_shop(state)

    assert action == BuyVoucher(1, reason="buy voucher: Grabber for $10 ($40->$30)")


def test_choose_buy_pack_in_shop_prioritizes_constellation_celestial() -> None:
    state = {
        "money": 30,
        "ante_num": 3,
        "jokers": {"count": 1, "limit": 5, "cards": [joker("j_constellation", "Constellation")]},
        "packs": {
            "cards": [
                {"label": "Arcana Pack", "cost": {"buy": 4}},
                {"label": "Celestial Pack", "cost": {"buy": 4}},
            ],
        },
    }

    action = choose_buy_pack_in_shop(state)

    assert action == BuyPack(1, reason="buy pack: Celestial Pack for $4 ($30->$26)")


def test_buy_vouchers_rule_delegates_to_policy() -> None:
    state = {
        "money": 40,
        "vouchers": {
            "cards": [
                {"key": "v_grabber", "label": "Grabber", "cost": {"buy": 10}},
            ],
        },
    }

    action = BuyVouchersInShop().evaluate(state)

    assert action == BuyVoucher(0, reason="buy voucher: Grabber for $10 ($40->$30)")


def test_buy_packs_rule_delegates_to_policy() -> None:
    state = {
        "money": 30,
        "ante_num": 3,
        "jokers": {"count": 1, "limit": 5, "cards": [joker("j_constellation", "Constellation")]},
        "packs": {
            "cards": [
                {"label": "Arcana Pack", "cost": {"buy": 4}},
                {"label": "Celestial Pack", "cost": {"buy": 4}},
            ],
        },
    }

    action = BuyPacksInShop().evaluate(state)

    assert action == BuyPack(1, reason="buy pack: Celestial Pack for $4 ($30->$26)")


def test_choose_reroll_shop_uses_strategy_when_no_buyable_joker_exists() -> None:
    state = {
        "money": 40,
        "hands": {"Pair": {"level": 1, "chips": 10, "mult": 10}},
        "jokers": {"count": 0, "limit": 5, "cards": []},
        "shop": {"cards": [{"set": "JOKER", "key": "j_nonexistent_xyz", "cost": {"buy": 4}}]},
    }

    action = choose_reroll_shop(state, 0, min_money_to_reroll=35, max_rerolls=3)

    assert action == Reroll(reason="reroll shop ($40, looking for no strategy yet jokers)")


def test_reroll_shop_rule_delegates_to_policy_and_tracks_counter() -> None:
    rule = RerollShop()
    state = {
        "round_num": 5,
        "money": 40,
        "hands": {"Pair": {"level": 1, "chips": 10, "mult": 10}},
        "jokers": {"count": 0, "limit": 5, "cards": []},
        "shop": {"cards": [{"set": "JOKER", "key": "j_nonexistent_xyz", "cost": {"buy": 4}}]},
    }

    first = rule.evaluate(state)
    second = rule.evaluate(state)

    assert first == Reroll(reason="reroll shop ($40, looking for no strategy yet jokers)")
    assert second == Reroll(reason="reroll shop ($40, looking for no strategy yet jokers)")
    assert rule._rerolls_this_shop == 2


def test_choose_leave_shop_returns_next_round() -> None:
    action = choose_leave_shop()

    assert action == NextRound(reason="done shopping")


def test_leave_shop_rule_delegates_to_policy() -> None:
    action = LeaveShop().evaluate({})

    assert action == NextRound(reason="done shopping")
