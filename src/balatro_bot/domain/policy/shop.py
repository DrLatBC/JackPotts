"""Shop-phase policy functions — pure decision logic extracted from rules."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from balatro_bot.actions import (
    Action, BuyCard, BuyPack, BuyVoucher, NextRound, Reroll,
    SellConsumable, SellJoker,
)
from balatro_bot.constants import (
    PLANET_KEYS, SAFE_CONSUMABLE_TAROTS, SAFE_SPECTRAL_CONSUMABLES,
    SCALING_JOKERS, SCALING_XMULT, SPECTRAL_TARGETING, TARGETING_TAROTS,
)
from balatro_bot.joker_effects import JOKER_EFFECTS, _noop, parse_effect_value
from balatro_bot.domain.policy.shop_valuation import evaluate_joker_value
from balatro_bot.rules._helpers import evaluate_hex
from balatro_bot.strategy import JOKER_HAND_AFFINITY, compute_strategy

if TYPE_CHECKING:
    from typing import Any

log = logging.getLogger("balatro_bot")

# ── Constants ────────────────────────────────────────────────────────

INTEREST_CAP = 25
MIN_VALUE = 1.5

ALWAYS_BUY = {
    "j_cavendish", "j_stencil",
    "j_duo", "j_trio", "j_family", "j_order", "j_tribe",
    "j_gros_michel", "j_popcorn",
    "j_acrobat", "j_blackboard", "j_flower_pot",
    "j_madness",
}

HIGH_PRIORITY = {
    "j_constellation",
    "j_campfire",
}

HIGH_VALUE_XMULT = {
    "j_cavendish", "j_stencil",
    "j_duo", "j_trio", "j_family", "j_order", "j_tribe",
    "j_acrobat", "j_blackboard", "j_flower_pot",
    "j_madness",
    "j_constellation", "j_campfire",
}

VOUCHER_PRIORITY: dict[str, int] = {
    "v_grabber": 1, "v_nacho_tong": 1,
    "v_paint_brush": 2, "v_palette": 2,
    "v_wasteful": 3, "v_recyclomancy": 3,
    "v_antimatter": 4,
    "v_crystal_ball": 5,
    "v_hieroglyph": 6,
    "v_seed_money": 7, "v_money_tree": 7,
    "v_clearance_sale": 8, "v_overstock": 8,
}

PACK_PRIORITY_MAP = {
    "Celestial": 1,
    "Buffoon": 2,
    "Arcana": 3,
}

ALL_PACK_KEYWORDS = {"Celestial", "Buffoon", "Arcana", "Standard", "Spectral"}


# ── Helpers ──────────────────────────────────────────────────────────

def _interest_after(money: int, cost: int) -> int:
    return min((money - cost) // 5, 5)


def _pack_priority(label: str) -> int | None:
    for keyword, priority in PACK_PRIORITY_MAP.items():
        if keyword in label:
            return priority
    return None


# ── Policy functions ─────────────────────────────────────────────────

def choose_sell_weak_joker(state: dict[str, Any]) -> Action | None:
    """Sell the weakest joker if decayed or if shop has a better upgrade."""
    joker_info = state.get("jokers", {})
    owned = joker_info.get("cards", [])

    # Proactive sell: Popcorn decayed to ≤4 mult
    for i, j in enumerate(owned):
        if j.get("key") == "j_popcorn":
            effect_text = j.get("value", {}).get("effect", "")
            parsed = parse_effect_value(effect_text) if effect_text else {}
            current_mult = parsed.get("mult", 99)
            if current_mult <= 4:
                return SellJoker(
                    i, reason=f"sell decayed Popcorn (+{current_mult} Mult, about to disappear)"
                )

    # Proactive sell: Ramen decayed below X1.0
    for i, j in enumerate(owned):
        if j.get("key") == "j_ramen":
            effect_text = j.get("value", {}).get("effect", "")
            parsed = parse_effect_value(effect_text) if effect_text else {}
            current_xmult = parsed.get("xmult")
            if current_xmult is not None and current_xmult < 1.0:
                return SellJoker(
                    i, reason=f"sell decayed Ramen (X{current_xmult:.2f}, reducing scores)"
                )

    # Only consider upgrade-selling when slots are full
    if joker_info.get("count", 0) < joker_info.get("limit", 5):
        return None
    if not owned:
        return None

    hand_levels = state.get("hands", {})
    strat = compute_strategy(owned, hand_levels)
    if not strat.preferred_hands:
        return None

    shop = state.get("shop", {})
    ante = state.get("ante_num", 1)
    shop_has_xmult_buy = any(
        card.get("key") in HIGH_VALUE_XMULT
        for card in shop.get("cards", [])
        if card.get("set") == "JOKER"
    )

    always_protected = {"j_madness", "j_ceremonial"} | SCALING_XMULT
    if shop_has_xmult_buy:
        protected = always_protected
    else:
        protected = always_protected | SCALING_JOKERS

    def _is_stale_scaler(j: dict, cur_ante: int) -> bool:
        key = j.get("key", "")
        if key not in SCALING_JOKERS or key in always_protected:
            return False
        effect_text = j.get("value", {}).get("effect", "")
        parsed = parse_effect_value(effect_text) if effect_text else {}
        chips = parsed.get("chips") or 0
        mult = parsed.get("mult") or 0
        if cur_ante >= 4 and chips <= 20 and mult <= 5:
            return True
        if cur_ante >= 6 and chips <= 50 and mult <= 10:
            return True
        return False

    owned_values = [
        (i, evaluate_joker_value(j, owned_jokers=owned,
                                 hand_levels=hand_levels, ante=ante, strategy=strat), j)
        for i, j in enumerate(owned)
        if j.get("key") not in protected or _is_stale_scaler(j, ante)
    ]
    if not owned_values:
        return None

    weakest_idx, weakest_value, weakest_joker = min(owned_values, key=lambda x: x[1])

    money = state.get("money", 0)
    sell_value = weakest_joker.get("cost", {}).get("sell", 0)
    current_interest = min(money // 5, 5)

    best_sell_target = None
    best_shop_value = -1.0
    best_shop_label = ""
    best_threshold = 0.0

    weakest_key = weakest_joker.get("key", "")
    weakest_on_strategy = any(
        strat.hand_affinity(ht) > 0
        for ht in JOKER_HAND_AFFINITY.get(weakest_key, ([], 0))[0]
    )

    for card in shop.get("cards", []):
        if card.get("set") != "JOKER":
            continue
        cost = card.get("cost", {}).get("buy", 999)
        money_after_sell = money + sell_value
        if cost > money_after_sell:
            continue

        if ante < 5 and money_after_sell < INTEREST_CAP:
            interest_after_buy = min((money_after_sell - cost) // 5, 5)
            if interest_after_buy < current_interest:
                continue

        shop_value = evaluate_joker_value(card, owned_jokers=owned,
                                          hand_levels=hand_levels, ante=ante, strategy=strat)
        shop_key = card.get("key", "")
        is_high_tier_xmult = shop_key in HIGH_VALUE_XMULT

        shop_on_strategy = any(
            strat.hand_affinity(ht) > 0
            for ht in JOKER_HAND_AFFINITY.get(shop_key, ([], 0))[0]
        )

        if is_high_tier_xmult:
            threshold = 0.5
        elif weakest_on_strategy and not shop_on_strategy:
            threshold = 3.0
        elif weakest_on_strategy and shop_on_strategy:
            threshold = 1.5
        else:
            threshold = 1.0

        if shop_value > weakest_value + threshold and shop_value > best_shop_value:
            best_sell_target = card
            best_shop_value = shop_value
            best_shop_label = card.get("label", "?")
            best_threshold = threshold

    if best_sell_target is not None:
        return SellJoker(
            weakest_idx,
            reason=f"sell {weakest_joker.get('label', '?')} (value={weakest_value:.1f}) "
                   f"for {best_shop_label} (value={best_shop_value:.1f}, threshold={best_threshold:.1f}) "
                   f"[strategy: {', '.join(n for n, _ in strat.preferred_hands[:2])}]",
        )

    return None


def choose_sell_diet_cola(state: dict[str, Any]) -> Action | None:
    """Sell Diet Cola for a free reroll when nothing good is in the shop."""
    owned = state.get("jokers", {}).get("cards", [])

    diet_idx = next(
        (i for i, j in enumerate(owned) if j.get("key") == "j_diet_cola"), None
    )
    if diet_idx is None:
        return None

    shop = state.get("shop", {})
    for card in shop.get("cards", []):
        if card.get("set") == "JOKER":
            key = card.get("key", "")
            if key in ALWAYS_BUY or key in HIGH_PRIORITY:
                return None

    return SellJoker(diet_idx, reason="Diet Cola: sell for free shop reroll")


def choose_feed_campfire(state: dict[str, Any]) -> Action | None:
    """Sell consumables to feed Campfire's X0.25 Mult per sell."""
    owned_jokers = state.get("jokers", {}).get("cards", [])
    if not any(j.get("key") == "j_campfire" for j in owned_jokers):
        return None

    consumables = state.get("consumables", {}).get("cards", [])
    if not consumables:
        return None

    hand_levels = state.get("hands", {})
    strat = compute_strategy(owned_jokers, hand_levels)

    all_useful = (
        SAFE_CONSUMABLE_TAROTS
        | set(TARGETING_TAROTS)
        | SAFE_SPECTRAL_CONSUMABLES
        | set(SPECTRAL_TARGETING)
    )

    for i, card in enumerate(consumables):
        key = card.get("key", "")

        if key in PLANET_KEYS:
            hand_type = PLANET_KEYS[key]
            if hand_type == "ALL":
                continue
            if strat.hand_affinity(hand_type) > 0:
                continue
            return SellConsumable(
                i,
                reason=f"Campfire: sell {card.get('label', '?')} (+X0.25 Mult, {hand_type} has no affinity)",
            )

        if key == "c_hex":
            ante = state.get("ante_num", 1)
            hex_score = evaluate_hex(owned_jokers, ante, hand_levels)
            if hex_score <= 0.0:
                return SellConsumable(
                    i, reason="Campfire: sell Hex (+X0.25 Mult, not worth using)",
                )

        if key in all_useful:
            continue

        return SellConsumable(
            i,
            reason=f"Campfire: sell {card.get('label', '?')} (+X0.25 Mult)",
        )

    return None


def choose_buy_joker_in_shop(state: dict[str, Any]) -> Action | None:
    """Buy the best joker from the shop, respecting interest and slot pressure."""
    money = state.get("money", 0)
    shop = state.get("shop", {})
    joker_slots = state.get("jokers", {})
    ante = state.get("ante_num", 1)

    if joker_slots.get("count", 0) >= joker_slots.get("limit", 5):
        for card in shop.get("cards", []):
            if card.get("set") != "JOKER":
                continue
            key = card.get("key", "")
            if key in ALWAYS_BUY or key in HIGH_PRIORITY:
                label = card.get("label", "?")
                cost = card.get("cost", {}).get("buy", 999)
                log.info("[SHOP] %s($%d): slots full (%d/%d) — can't buy",
                         label, cost, joker_slots.get("count", 0), joker_slots.get("limit", 5))
        return None

    current_interest = min(money // 5, 5)
    strat = compute_strategy(joker_slots.get("cards", []), state.get("hands", {}))
    jlimit = joker_slots.get("limit", 5)

    best_idx = None
    best_improvement = 0.0
    best_cost = 0
    best_label = ""
    passed_on: list[str] = []

    for i, card in enumerate(shop.get("cards", [])):
        if card.get("set") != "JOKER":
            continue
        label = card.get("label", "?")
        cost = card.get("cost", {}).get("buy", 999)
        if cost > money:
            passed_on.append(f"{label}(${cost}, can't afford)")
            continue

        key = card.get("key", "")
        joker_count = joker_slots.get("count", 0)
        owned_keys = {j.get("key") for j in joker_slots.get("cards", [])}

        from balatro_bot.scaling import check_anti_synergy
        blocker = check_anti_synergy(key, owned_keys)
        if blocker:
            passed_on.append(f"{label}(${cost}, conflicts with {blocker})")
            continue

        if key == "j_madness":
            if owned_keys & SCALING_JOKERS:
                passed_on.append(f"{label}(${cost}, would eat scaling joker)")
                continue
        if key in SCALING_JOKERS:
            if "j_madness" in owned_keys:
                passed_on.append(f"{label}(${cost}, Madness would eat it)")
                continue

        SLOW_SCALERS = (SCALING_JOKERS | {"j_madness", "j_ceremonial"}) - SCALING_XMULT
        if key in SLOW_SCALERS and ante >= 6:
            passed_on.append(f"{label}(${cost}, too late for slow scaler at ante {ante})")
            continue

        value = evaluate_joker_value(
            card, owned_jokers=joker_slots.get("cards", []),
            hand_levels=state.get("hands", {}), ante=ante,
            strategy=strat, joker_limit=jlimit,
        )

        force_buy = False
        if key in ALWAYS_BUY:
            value = max(value, 10.0)
            force_buy = True
        elif key in HIGH_PRIORITY and ante <= 5:
            value = max(value, 8.0)
            force_buy = True

        if any(j.get("key") == "j_stencil" for j in joker_slots.get("cards", [])):
            stencil_count = sum(1 for j in joker_slots.get("cards", []) if j.get("key") == "j_stencil")
            stencil_mult_after = (jlimit - joker_count - 1) + stencil_count
            if stencil_mult_after <= 2 and value < 5.0:
                passed_on.append(f"{label}(${cost}, Stencil restriction: only ×{stencil_mult_after} left)")
                continue

        if joker_count == 0:
            value = max(value, 5.0)
            force_buy = True

        if not force_buy and 3 <= ante <= 4 and joker_count > 2:
            interest_after = _interest_after(money, cost)
            loses_interest = interest_after < current_interest

            if loses_interest:
                if money >= INTEREST_CAP:
                    if value < MIN_VALUE:
                        passed_on.append(f"{label}(${cost}, value={value:.1f} below threshold)")
                        continue
                else:
                    if cost > 2:
                        passed_on.append(f"{label}(${cost}, saving for interest)")
                        continue

        if joker_count >= jlimit - 1:
            value *= 0.5

        if value > best_improvement:
            best_improvement = value
            best_idx = i
            best_cost = cost
            best_label = card.get("label", "?")

    if best_idx is not None:
        key = shop.get("cards", [])[best_idx].get("key", "")
        tier = "ALWAYS_BUY" if key in ALWAYS_BUY else (
            "HIGH_PRIORITY" if key in HIGH_PRIORITY else "scored")
        log.info("[SHOP] %s($%d): %s, value=%.1f — BUYING",
                 best_label, best_cost, tier, best_improvement)
        return BuyCard(
            best_idx,
            reason=f"buy joker: {best_label} for ${best_cost} "
                   f"(value={best_improvement:.1f}, ${money}->${money - best_cost})",
        )
    if passed_on:
        log.info("Passed on jokers: %s", ", ".join(passed_on))
    return None


def choose_buy_consumable_in_shop(state: dict[str, Any]) -> Action | None:
    """Buy the best consumable from the shop."""
    from balatro_bot.rules._helpers import score_consumable

    money = state.get("money", 0)
    shop = state.get("shop", {})
    consumables = state.get("consumables", {})

    if consumables.get("count", 0) >= consumables.get("limit", 2):
        return None

    jokers = state.get("jokers", {}).get("cards", [])
    hand_levels = state.get("hands", {})
    strat = compute_strategy(jokers, hand_levels)

    best_idx = None
    best_value = 0.0
    best_cost = 0
    best_label = ""
    passed_on: list[str] = []

    for i, card in enumerate(shop.get("cards", [])):
        key = card.get("key", "")
        label = card.get("label", "?")
        card_set = card.get("set", "")
        cost = card.get("cost", {}).get("buy", 999)

        if card_set not in ("TAROT", "PLANET", "SPECTRAL") and key not in PLANET_KEYS:
            continue

        value = score_consumable(key, state, strat)
        if value <= 0:
            passed_on.append(f"{label}(value={value:.1f})")
            continue

        if cost > money:
            passed_on.append(f"{label}(${cost}, can't afford)")
            continue

        current_interest = min(money // 5, 5)
        if money < INTEREST_CAP:
            interest_after = min((money - cost) // 5, 5)
            if interest_after < current_interest and cost > 3:
                passed_on.append(f"{label}(${cost}, saving for interest)")
                continue

        if value > best_value or (value == best_value and cost < best_cost):
            best_value = value
            best_idx = i
            best_cost = cost
            best_label = label

    if best_idx is not None:
        log.info("[SHOP] buy consumable: %s ($%d, value=%.1f)", best_label, best_cost, best_value)
        return BuyCard(
            best_idx,
            reason=f"buy consumable: {best_label} for ${best_cost} (value={best_value:.1f}, ${money}->${money - best_cost})",
        )
    if passed_on:
        log.info("Passed on consumables: %s", ", ".join(passed_on))
    return None


def choose_buy_voucher_in_shop(state: dict[str, Any]) -> Action | None:
    """Buy highest-priority affordable voucher."""
    money = state.get("money", 0)
    vouchers = state.get("vouchers", {})

    if money < INTEREST_CAP + 10:
        return None

    best_idx = None
    best_priority = 999
    best_cost = 0
    best_label = ""

    for i, card in enumerate(vouchers.get("cards", [])):
        key = card.get("key", "")
        priority = VOUCHER_PRIORITY.get(key)
        if priority is None:
            continue

        cost = card.get("cost", {}).get("buy", 999)
        if cost > money - INTEREST_CAP:
            continue

        if priority < best_priority:
            best_priority = priority
            best_idx = i
            best_cost = cost
            best_label = card.get("label", "?")

    if best_idx is not None:
        return BuyVoucher(
            best_idx,
            reason=f"buy voucher: {best_label} for ${best_cost} (${money}->${money - best_cost})",
        )
    return None


def choose_buy_pack_in_shop(state: dict[str, Any]) -> Action | None:
    """Buy best pack, prioritizing Celestial > Buffoon > Arcana."""
    money = state.get("money", 0)
    packs = state.get("packs", {})
    joker_slots = state.get("jokers", {})
    owned_jokers = joker_slots.get("cards", [])
    has_red_card = any(j.get("key") == "j_red_card" for j in owned_jokers)
    has_constellation = any(j.get("key") == "j_constellation" for j in owned_jokers)

    best_idx = None
    best_priority = 999
    best_cost = 0
    best_label = ""
    passed_on: list[str] = []

    for i, card in enumerate(packs.get("cards", [])):
        label = card.get("label", "")
        cost = card.get("cost", {}).get("buy", 999)
        if cost > money:
            passed_on.append(f"{label}(${cost}, can't afford)")
            continue

        if has_red_card and any(kw in label for kw in ALL_PACK_KEYWORDS):
            if cost <= 4 or money >= INTEREST_CAP:
                priority = 0
            else:
                passed_on.append(f"{label}(${cost}, Red Card but too expensive)")
                continue
        elif has_constellation and "Celestial" in label:
            priority = 0
        else:
            priority = _pack_priority(label)
            if priority is None:
                if "Spectral" in label and state.get("ante_num", 1) >= 3:
                    priority = 4
                else:
                    passed_on.append(f"{label}(${cost}, not in buy list)")
                    continue

        if "Buffoon" in label and not has_red_card:
            if joker_slots.get("count", 0) >= joker_slots.get("limit", 5):
                passed_on.append(f"{label}(${cost}, joker slots full)")
                continue

        skip_interest = has_red_card or (has_constellation and "Celestial" in label)
        if not skip_interest:
            current_interest = min(money // 5, 5)
            interest_after = _interest_after(money, cost)
            loses_interest = interest_after < current_interest
            if loses_interest:
                if money >= INTEREST_CAP:
                    if "Celestial" not in label:
                        passed_on.append(f"{label}(${cost}, would lose interest)")
                        continue
                else:
                    passed_on.append(f"{label}(${cost}, saving for interest)")
                    continue

        if priority < best_priority:
            best_priority = priority
            best_idx = i
            best_cost = cost
            best_label = label

    if best_idx is not None:
        reason = f"buy pack: {best_label} for ${best_cost} (${money}->${money - best_cost})"
        if has_red_card:
            reason += " [Red Card: +3 mult on skip]"
        return BuyPack(best_idx, reason=reason)
    if passed_on:
        log.info("Passed on packs: %s", ", ".join(passed_on))
    return None


def choose_reroll_shop(
    state: dict[str, Any],
    reroll_counter: int,
    min_money_to_reroll: int = 35,
    max_rerolls: int = 3,
) -> Action | None:
    """Reroll when flush with cash and nothing good is available."""
    money = state.get("money", 0)

    if reroll_counter >= max_rerolls:
        return None
    if money < min_money_to_reroll:
        return None

    shop = state.get("shop", {})
    joker_slots = state.get("jokers", {})
    has_open_slots = joker_slots.get("count", 0) < joker_slots.get("limit", 5)

    if has_open_slots:
        for card in shop.get("cards", []):
            if card.get("set") == "JOKER":
                key = card.get("key", "")
                effect = JOKER_EFFECTS.get(key)
                if effect is not None and effect is not _noop:
                    cost = card.get("cost", {}).get("buy", 999)
                    if cost <= money:
                        return None

    strat = compute_strategy(
        state.get("jokers", {}).get("cards", []), state.get("hands", {})
    )
    strat_str = ", ".join(n for n, _ in strat.preferred_hands[:2]) if strat.preferred_hands else "no strategy yet"
    return Reroll(reason=f"reroll shop (${money}, looking for {strat_str} jokers)")


def choose_leave_shop() -> NextRound:
    """Leave the shop."""
    return NextRound(reason="done shopping")
