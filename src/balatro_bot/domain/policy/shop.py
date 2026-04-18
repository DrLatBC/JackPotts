"""Shop-phase helpers kept for external consumers (pack rules / pack policy)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from balatro_bot.domain.models.deck_profile import DeckProfile

if TYPE_CHECKING:
    from typing import Any


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
