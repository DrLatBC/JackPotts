"""Shop-phase policy functions — kept functions still referenced externally."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from balatro_bot.actions import Action, BuyCard
from balatro_bot.constants import PLANET_KEYS
from balatro_bot.cards import joker_key
from balatro_bot.domain.models.deck_profile import DeckProfile
from balatro_bot.strategy import compute_strategy

if TYPE_CHECKING:
    from typing import Any

log = logging.getLogger("balatro_bot")


def _get_deck_profile(state: dict[str, Any]) -> DeckProfile:
    """Get or build+cache a DeckProfile from the raw state dict."""
    cached = state.get("_deck_profile")
    if cached is not None:
        return cached
    from balatro_bot.domain.models.card import card_from_dict
    deck_cards = [card_from_dict(c) for c in state.get("cards", {}).get("cards", [])]
    hand_cards = [card_from_dict(c) for c in state.get("hand", {}).get("cards", [])]
    profile = DeckProfile.from_cards(deck_cards + hand_cards)
    state["_deck_profile"] = profile
    return profile


# ── Tuning constants ─────────────────────────────────────────────────

INTEREST_CAP = 25       # max money that earns interest ($5/round at $25)


def _get_edition(card: dict) -> str | None:
    """Return the edition string for a card, or None."""
    mod = card.get("modifier")
    return mod.get("edition") if isinstance(mod, dict) else None


def _is_negative(card: dict) -> bool:
    """True if the card has Negative edition (+1 joker slot, no scoring bonus)."""
    return _get_edition(card) == "NEGATIVE"


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


# ── Helpers ──────────────────────────────────────────────────────────

def _interest_after(money: int, cost: int) -> int:
    return min((money - cost) // 5, 5)


# ── Kept policy functions ────────────────────────────────────────────

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

        current_interest = _interest_after(money, 0)
        if money < INTEREST_CAP:
            interest_after = _interest_after(money, cost)
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
