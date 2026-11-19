"""Shop-phase helpers shared by the shop evaluator, pack rules and pack policy."""

from __future__ import annotations


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
