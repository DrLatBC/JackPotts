"""Playing-phase policy functions — pure decision logic extracted from rules."""

from __future__ import annotations

from typing import TYPE_CHECKING

from balatro_bot.actions import Action, SellJoker
from balatro_bot.cards import is_debuffed, joker_key
from balatro_bot.domain.models.joker import Joker
from balatro_bot.scaling import SELL_PROTECTED


def _sell_cost(j: Joker | dict) -> int:
    if isinstance(j, Joker):
        return j.cost.get("sell", 99) if isinstance(j.cost, dict) else 99
    return j.get("cost", {}).get("sell", 99)


def _joker_label(j: Joker | dict) -> str:
    if isinstance(j, Joker):
        return j.label or "?"
    return j.get("label", "?")

if TYPE_CHECKING:
    from balatro_bot.context import RoundContext

# Boss blind names — Luchador only matters against these
BOSS_BLINDS = {
    "The Needle", "The Eye", "The Mouth", "The Psychic",
    "Crimson Heart", "The Flint", "The Plant", "The Head",
    "The Water", "The Window", "The Hook", "The Wall",
    "The Wheel", "The Arm", "The Club", "The Fish",
    "The Tooth", "The Mark", "The Ox", "The House",
    "The Pillar", "The Serpent", "The Goad", "Amber Acorn",
    "Verdant Leaf", "Violet Vessel", "Cerulean Bell",
}


def choose_verdant_leaf_unlock(ctx: RoundContext) -> Action | None:
    """Sell weakest joker to lift Verdant Leaf debuff."""
    if ctx.blind_name != "Verdant Leaf":
        return None
    if not any(is_debuffed(c) for c in ctx.hand_cards):
        return None
    candidates = [
        (i, j) for i, j in enumerate(ctx.jokers)
        if joker_key(j) not in SELL_PROTECTED
    ]
    if not candidates:
        candidates = list(enumerate(ctx.jokers))
    if not candidates:
        return None
    sell_idx = min(candidates, key=lambda x: _sell_cost(x[1]))[0]
    label = _joker_label(ctx.jokers[sell_idx])
    return SellJoker(sell_idx, reason=f"Verdant Leaf: sell {label} to unlock debuffed cards")


def choose_sell_luchador(ctx: RoundContext) -> Action | None:
    """Sell Luchador to disable a boss blind when losing."""
    if ctx.blind_name not in BOSS_BLINDS:
        return None

    luchador_idx = next(
        (i for i, j in enumerate(ctx.jokers) if joker_key(j) == "j_luchador"), None
    )
    if luchador_idx is None:
        return None

    if ctx.chips_scored == 0 and ctx.hands_left > 1:
        return None

    best_score = ctx.best.total * ctx.score_discount if ctx.best else 0
    projected = best_score * ctx.hands_left
    if projected >= ctx.chips_remaining:
        return None

    return SellJoker(
        luchador_idx,
        reason=f"Luchador: sell to disable {ctx.blind_name} "
               f"(projected {projected:.0f} < {ctx.chips_remaining} needed)",
    )
