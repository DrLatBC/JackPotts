from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from balatro_bot.actions import RearrangeJokers, Action
from balatro_bot.cards import joker_key
from balatro_bot.context import RoundContext
from balatro_bot.scaling import BLUEPRINT_INCOMPATIBLE

if TYPE_CHECKING:
    from typing import Any

log = logging.getLogger("balatro_bot")


class ReorderJokersForScoring:
    """Arrange jokers in optimal scoring order: +chips -> +mult -> xmult (left to right).

    Also handles position-dependent jokers:
    - Blueprint: placed immediately left of the best xmult joker to copy
    - Brainstorm: ensures leftmost joker is a good copyable effect
    - Ceremonial Dagger: placed immediately left of fodder to sacrifice

    Subsumes the old ReorderJokersForCeremonial rule.
    """
    name = "reorder_jokers_for_scoring"

    def __init__(self) -> None:
        self._last_order: list[int] | None = None

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        from balatro_bot.joker_effects.scoring_phase import (
            get_joker_phase, get_joker_edition_phase,
            PHASE_NOOP, PHASE_CHIPS, PHASE_MULT, PHASE_XMULT,
        )

        joker_info = state.get("jokers", {})
        owned = joker_info.get("cards", [])
        if len(owned) < 2:
            self._last_order = None
            return None

        # Amber Acorn reshuffles jokers every hand — reordering is futile
        ctx = RoundContext.from_state(state)
        if ctx.blind_name == "Amber Acorn":
            return None

        # --- Classify each joker ---
        ceremonial_idx: int | None = None
        blueprint_idx: int | None = None
        brainstorm_idx: int | None = None
        fodder: list[int] = []  # noop jokers (candidates for Ceremonial sacrifice)

        for i, j in enumerate(owned):
            key = joker_key(j)
            phase = get_joker_phase(key)
            if key == "j_ceremonial":
                ceremonial_idx = i
            elif key == "j_blueprint":
                blueprint_idx = i
            elif key == "j_brainstorm":
                brainstorm_idx = i
            if phase == PHASE_NOOP:
                fodder.append(i)

        # --- Sort all jokers by scoring phase ---
        # Blueprint and Brainstorm are excluded (inserted with constraints after).
        # Ceremonial participates in the sort as +mult (its scoring phase).
        # Primary: phase (0=noop, 1=chips, 2=mult, 3=xmult)
        # Secondary: edition phase (Polychrome jokers rightward within group)
        # Tertiary: original position (stability)
        excluded = {blueprint_idx, brainstorm_idx} - {None}
        sortable = []
        for i, j in enumerate(owned):
            if i in excluded:
                continue
            key = joker_key(j)
            phase = get_joker_phase(key)
            ed_phase = get_joker_edition_phase(j)
            sortable.append((i, phase, ed_phase))

        sortable.sort(key=lambda x: (x[1], x[2], x[0]))
        desired_order = [i for i, _, _ in sortable]

        # --- Ceremonial constraint: fodder must be immediately to its right ---
        # If no fodder exists, Ceremonial goes RIGHTMOST so it eats nothing.
        if ceremonial_idx is not None:
            available_fodder = [f for f in fodder if f != ceremonial_idx
                                and f not in excluded]
            # Always remove Ceremonial from its sorted position first
            desired_order = [i for i in desired_order if i != ceremonial_idx]
            if available_fodder:
                available_fodder.sort(key=lambda i: joker_key(owned[i]))
                sacrifice_idx = available_fodder[0]
                desired_order = [i for i in desired_order if i != sacrifice_idx]
                # Insert Ceremonial after the last +mult joker (or after chips)
                insert_at = 0
                for pos, idx in enumerate(desired_order):
                    if get_joker_phase(joker_key(owned[idx])) <= PHASE_MULT:
                        insert_at = pos + 1
                desired_order.insert(insert_at, ceremonial_idx)
                desired_order.insert(insert_at + 1, sacrifice_idx)
            else:
                # No fodder — Ceremonial goes rightmost (eats nothing)
                desired_order.append(ceremonial_idx)

        # --- Blueprint: place immediately left of best xmult joker to copy ---
        if blueprint_idx is not None:
            # Find rightmost xmult joker position (best copy target).
            # Skip copy-incompatible jokers (Blueprint produces nothing from them).
            best_target_pos = None
            for pos in range(len(desired_order) - 1, -1, -1):
                idx = desired_order[pos]
                key = joker_key(owned[idx])
                if key in ("j_blueprint", "j_brainstorm"):
                    continue
                if key in BLUEPRINT_INCOMPATIBLE:
                    continue
                phase = get_joker_phase(key)
                if phase == PHASE_XMULT:
                    best_target_pos = pos
                    break
            if best_target_pos is None:
                # No xmult — find rightmost +mult
                for pos in range(len(desired_order) - 1, -1, -1):
                    idx = desired_order[pos]
                    key = joker_key(owned[idx])
                    if key in BLUEPRINT_INCOMPATIBLE:
                        continue
                    if get_joker_phase(key) == PHASE_MULT:
                        best_target_pos = pos
                        break
            if best_target_pos is not None:
                desired_order.insert(best_target_pos, blueprint_idx)
            else:
                desired_order.append(blueprint_idx)

        # --- Brainstorm: copies leftmost joker, place anywhere except pos 0 ---
        if brainstorm_idx is not None:
            desired_order.append(brainstorm_idx)
            # Ensure position 0 is a good copyable effect (not noop/brainstorm/incompatible)
            if desired_order:
                first_key = joker_key(owned[desired_order[0]])
                first_phase = get_joker_phase(first_key)
                needs_swap = (
                    first_phase == PHASE_NOOP or first_key in BLUEPRINT_INCOMPATIBLE
                )
                if needs_swap and len(desired_order) > 1:
                    for swap_pos in range(1, len(desired_order)):
                        swap_key = joker_key(owned[desired_order[swap_pos]])
                        if (get_joker_phase(swap_key) != PHASE_NOOP
                                and swap_key != "j_brainstorm"
                                and swap_key not in BLUEPRINT_INCOMPATIBLE):
                            desired_order[0], desired_order[swap_pos] = \
                                desired_order[swap_pos], desired_order[0]
                            break

        # --- Check if reorder needed ---
        current_order = list(range(len(owned)))
        if desired_order == current_order:
            self._last_order = None
            return None

        # Cycle guard
        if self._last_order == desired_order:
            return None
        self._last_order = desired_order

        # Build description of the new order for logging
        phase_names = {PHASE_NOOP: "noop", PHASE_CHIPS: "+c", PHASE_MULT: "+m", PHASE_XMULT: "xm"}
        order_desc = " ".join(
            f"{owned[i].get('label', '?')}({phase_names.get(get_joker_phase(owned[i].get('key', '')), '?')})"
            for i in desired_order
        )
        return RearrangeJokers(
            order=desired_order,
            reason=f"scoring order: {order_desc}",
        )


class UnifiedShopRule:
    """Single rule that replaces all siloed shop rules.

    Delegates to ShopEvaluator which scores everything (roster, shop, packs,
    vouchers, consumables), plans a budget, enumerates composite action plans,
    and picks the best one.
    """
    name = "unified_shop"

    def __init__(self) -> None:
        self._evaluator = None  # lazy init to avoid circular imports

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        if self._evaluator is None:
            from balatro_bot.domain.policy.shop_evaluator import ShopEvaluator
            self._evaluator = ShopEvaluator()
        return self._evaluator.evaluate(state)
