from __future__ import annotations

import re

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


# ── Riff-Raff awareness tests ───────────────────────────────────────


def test_sell_dead_riff_raff_when_slots_full() -> None:
    """Riff-Raff should be sold when all joker slots are full (spawns can't trigger)."""
    state = {
        "jokers": {
            "count": 5,
            "limit": 5,
            "cards": [
                joker("j_riff_raff", "Riff-Raff"),
                joker("j_joker", "Joker"),
                joker("j_duo", "Duo"),
                joker("j_trio", "Trio"),
                joker("j_family", "Family"),
            ],
        },
    }

    action = choose_sell_weak_joker(state)

    assert action == SellJoker(0, reason="sell dead Riff-Raff (all joker slots full, spawns can't trigger)")


def test_sell_weak_common_when_riff_raff_owned_slots_tight() -> None:
    """When Riff-Raff is owned and count >= limit - 1, sell weak Common jokers."""
    state = {
        "jokers": {
            "count": 4,
            "limit": 5,
            "cards": [
                joker("j_riff_raff", "Riff-Raff"),
                joker("j_vagabond", "Vagabond", rarity=1),  # Common, value 0.5
                joker("j_duo", "Duo"),
                joker("j_trio", "Trio"),
            ],
        },
        "hands": {"Pair": {"level": 1, "chips": 10, "mult": 10}},
        "ante_num": 3,
    }

    action = choose_sell_weak_joker(state)

    assert isinstance(action, SellJoker)
    assert action.index == 1  # Vagabond
    assert "weak Common" in action.reason
    assert "Vagabond" in action.reason


def test_riff_raff_penalizes_buy_when_reserving_slots() -> None:
    """Buying a marginal joker should be penalized when Riff-Raff needs spawn slots."""
    # 3/5 jokers (including Riff-Raff), so Riff-Raff reserves 2 slots.
    # Buying into slot 4 leaves only 1 free < 2 reserved → 0.7x penalty.
    # Compare value with and without Riff-Raff to prove the penalty applies.
    base_state = {
        "money": 10,
        "ante_num": 2,
        "hands": {"Pair": {"level": 1, "chips": 10, "mult": 10}},
        "jokers": {
            "count": 3,
            "limit": 5,
            "cards": [
                joker("j_duo", "Duo"),
                joker("j_trio", "Trio"),
                joker("j_family", "Family"),
            ],
        },
        "shop": {
            "cards": [
                _shop_joker("j_vagabond", "Vagabond", 2),
            ],
        },
    }
    rr_state = {
        **base_state,
        "jokers": {
            "count": 3,
            "limit": 5,
            "cards": [
                joker("j_riff_raff", "Riff-Raff"),
                joker("j_duo", "Duo"),
                joker("j_trio", "Trio"),
            ],
        },
    }

    action_no_rr = choose_buy_joker_in_shop(base_state)
    action_with_rr = choose_buy_joker_in_shop(rr_state)

    # Both buy Vagabond, but the Riff-Raff version has a lower reported value
    assert isinstance(action_no_rr, BuyCard)
    assert isinstance(action_with_rr, BuyCard)
    # Extract values from reason strings via regex (resilient to format changes)
    no_rr_match = re.search(r"value=([\d.]+)", action_no_rr.reason)
    rr_match = re.search(r"value=([\d.]+)", action_with_rr.reason)
    assert no_rr_match and rr_match, "Could not parse value from reason strings"
    no_rr_val = float(no_rr_match.group(1))
    rr_val = float(rr_match.group(1))
    assert rr_val < no_rr_val, f"Riff-Raff penalty not applied: {rr_val} >= {no_rr_val}"


def test_riff_raff_no_penalty_when_no_reservation() -> None:
    """When Riff-Raff is not owned, no slot penalty applies."""
    state = {
        "money": 10,
        "ante_num": 2,
        "hands": {"Pair": {"level": 1, "chips": 10, "mult": 10}},
        "jokers": {
            "count": 2,
            "limit": 5,
            "cards": [
                joker("j_duo", "Duo"),
                joker("j_trio", "Trio"),
            ],
        },
        "shop": {
            "cards": [
                _shop_joker("j_popcorn", "Popcorn", 3, "+20 Mult"),
            ],
        },
    }

    # Without Riff-Raff, Popcorn (ALWAYS_BUY) should be bought normally
    action = choose_buy_joker_in_shop(state)

    assert isinstance(action, BuyCard)
    assert "Popcorn" in action.reason


def test_riff_raff_stencil_anti_synergy_bidirectional() -> None:
    """Riff-Raff and Stencil should block each other in both directions."""
    from balatro_bot.scaling import check_anti_synergy

    # Stencil blocks Riff-Raff (existing)
    assert check_anti_synergy("j_riff_raff", {"j_stencil"}) == "j_stencil"
    # Riff-Raff blocks Stencil (existing)
    assert check_anti_synergy("j_stencil", {"j_riff_raff"}) == "j_riff_raff"


# ── Baseball Card awareness tests ──────────���────────────────────────


def test_baseball_protects_uncommon_jokers_from_selling() -> None:
    """Uncommon jokers should be protected from selling when Baseball Card is owned."""
    # 5/5 slots: Baseball + 2 Uncommons + 2 others. Shop has a decent joker.
    # Without protection, the weakest Uncommon might get sold for an upgrade.
    state = {
        "jokers": {
            "count": 5,
            "limit": 5,
            "cards": [
                joker("j_baseball", "Baseball Card", rarity=2),
                joker("j_jolly", "Jolly Joker", rarity=2),
                joker("j_zany", "Zany Joker", rarity=2),
                joker("j_duo", "Duo"),
                joker("j_trio", "Trio"),
            ],
        },
        "hands": {"Pair": {"level": 1, "chips": 10, "mult": 10}},
        "ante_num": 3,
        "money": 10,
        "shop": {
            "cards": [
                _shop_joker("j_family", "Family", 4, "X2 Mult if played hand contains a Full House"),
            ],
        },
    }

    action = choose_sell_weak_joker(state)

    # With Baseball protection, Uncommon jokers (Jolly, Zany, Baseball) are protected.
    # The only sellable candidates are Duo and Trio (both high value) — no upgrade should happen.
    # If an action is returned, it should NOT sell an Uncommon joker.
    if action is not None:
        sold_key = state["jokers"]["cards"][action.index]["key"]
        assert sold_key not in ("j_baseball", "j_jolly", "j_zany"), \
            f"Should not sell Uncommon joker {sold_key} when Baseball is owned"
