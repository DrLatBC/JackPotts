from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from balatro_bot.actions import UseConsumable, SellConsumable, SellJoker, RearrangeHand, Action
from balatro_bot.cards import joker_key
from balatro_bot.constants import (
    PLANET_KEYS, SAFE_CONSUMABLE_TAROTS, TARGETING_TAROTS,
    SAFE_SPECTRAL_CONSUMABLES, SPECTRAL_TARGETING,
)
from balatro_bot.domain.scoring.search import best_hand
from balatro_bot.strategy import compute_strategy
from balatro_bot.domain.policy.consumable_policy import (
    score_use_now, score_hold, evaluate_hex,
)
from balatro_bot.domain.policy.shop_valuation import evaluate_joker_value

if TYPE_CHECKING:
    from typing import Any

log = logging.getLogger("balatro_bot")

# ---------------------------------------------------------------------------
# Staleness penalty — held consumables lose value over rounds
# ---------------------------------------------------------------------------
_STALENESS_THRESHOLD = 2          # rounds held before penalty kicks in
_STALENESS_PENALTY_PER_ROUND = 0.5  # hold_value penalty per extra round


class UseConsumables:
    """Unified consumable usage: scores use_now vs hold_value for each consumable.

    Orchestration stays here (hex sell-down state machine, staleness tracking,
    slot pressure). Scoring logic delegates to consumable_policy.
    """
    name = "use_consumables"

    # Track how many rounds a consumable has been held (key -> round count)
    _held_rounds: dict[str, int] = {}
    _last_round: int = -1
    # Track the last tarot/planet we used, so Fool knows what it copies
    _last_used_consumable: str | None = None
    # Hex sell-down state: sell weak jokers before using Hex
    _hex_selling_down: bool = False
    _hex_target_key: str | None = None
    # Consumables that were rejected by the API this round — skip them
    _blocked_consumables: set[int] = set()  # indices blocked this round
    _blocked_round: int = -1

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        consumables = state.get("consumables", {}).get("cards", [])
        if not consumables:
            return None

        hand_cards = state.get("hand", {}).get("cards", [])
        jokers = state.get("jokers", {}).get("cards", [])
        hand_levels = state.get("hands", {})
        joker_slots = state.get("jokers", {})
        ante = state.get("ante_num", 1)
        money = state.get("money", 0)
        rnd = state.get("round", {})
        round_num = state.get("round_num", 0)

        strat = compute_strategy(jokers, hand_levels)

        # Reset blocked consumables on new round
        if round_num != self._blocked_round:
            self._blocked_consumables = set()
            self._blocked_round = round_num

        # Track held consumable staleness
        if round_num != self._last_round:
            self._last_round = round_num
            for card in consumables:
                key = card.get("key", "")
                self._held_rounds[key] = self._held_rounds.get(key, 0) + 1
            # Clean up keys no longer in consumable slots
            held_keys = {c.get("key", "") for c in consumables}
            for k in list(self._held_rounds):
                if k not in held_keys:
                    del self._held_rounds[k]

        # Compute round context for tactical decisions
        discards_left = rnd.get("discards_left", 0)
        hands_left = rnd.get("hands_left", 1)
        blind_score = 0
        blind_name: str | None = None
        chips_scored = rnd.get("chips", 0)
        for b in state.get("blinds", {}).values():
            if isinstance(b, dict) and b.get("status") == "CURRENT":
                blind_score = b.get("score", 0)
                blind_name = b.get("name")
                break
        chips_remaining = max(0, blind_score - chips_scored)

        joker_limit = joker_slots.get("limit", 5)
        current_best = None
        current_score = 0
        if hand_cards:
            current_best = best_hand(hand_cards, hand_levels, jokers=jokers,
                                     money=money, discards_left=discards_left,
                                     hands_left=hands_left, joker_limit=joker_limit)
            current_score = current_best.total if current_best else 0

        can_win_now = current_score >= chips_remaining
        # Desperate = genuinely about to lose: last 2 hands AND can't win yet.
        # Old code fired desperate paths on any hands_left<=2, wasting tarots
        # on rounds we were already crushing.
        desperate = hands_left <= 2 and not can_win_now
        consumable_limit = state.get("consumables", {}).get("limit", 2)
        slots_full = len(consumables) >= consumable_limit

        # Hex sell-down: if mid-sequence, continue selling before anything else
        hex_action = self._handle_hex_selldown(consumables, jokers, hand_levels, ante, blind_name)
        if hex_action is not None:
            return hex_action

        # Score each consumable: use_value vs hold_value
        candidates: list[tuple[int, float, str, Action]] = []

        for i, card in enumerate(consumables):
            if i in self._blocked_consumables:
                continue
            key = card.get("key", "")
            label = card.get("label", "?")

            # --- Special cases that bypass scoring ---

            # Hex: evaluate whether to sell, use, or start sell-down
            if key == "c_hex":
                hex_score = evaluate_hex(jokers, ante, hand_levels, blind_name=blind_name)
                if hex_score <= 0.0:
                    return SellConsumable(i, reason="sell Hex (not worth using — lineup too valuable)")
                if len(jokers) == 1:
                    # Single joker — use immediately if no edition
                    if not self._has_edition(jokers[0]):
                        return UseConsumable(i, reason=f"use Hex on {jokers[0].get('label', '?')} (solo joker, free Polychrome)")
                    else:
                        return SellConsumable(i, reason="sell Hex (only joker already has edition)")
                if len(jokers) > 1:
                    # Multiple jokers — start sell-down sequence
                    target = self._find_hex_target(jokers, hand_levels, ante, blind_name)
                    self._hex_selling_down = True
                    self._hex_target_key = target.get("key")
                    # Sell the weakest non-target joker this tick
                    return self._handle_hex_selldown(consumables, jokers, hand_levels, ante, blind_name)

            # --- Compute use_value and hold_value via policy ---
            use_value, action_args = score_use_now(
                i, key, label, card, state, strat, hand_cards, jokers,
                hand_levels, current_best, current_score, chips_remaining,
                can_win_now, desperate, money, discards_left, hands_left,
                joker_limit, ante,
                last_used_consumable=self._last_used_consumable,
            )
            hold_value = score_hold(
                key, ante, slots_full, desperate, hands_left, discards_left,
            )

            # Staleness penalty
            rounds_held = self._held_rounds.get(key, 0)
            if rounds_held >= _STALENESS_THRESHOLD:
                hold_value -= (rounds_held - 1) * _STALENESS_PENALTY_PER_ROUND

            if use_value > hold_value and action_args is not None:
                action = self._action_from_args(action_args)
                candidates.append((i, use_value, label, action))

        if not candidates:
            # Sell stale unknown consumables to free slots
            if slots_full:
                all_known = (PLANET_KEYS.keys() | SAFE_CONSUMABLE_TAROTS |
                             TARGETING_TAROTS.keys() | SAFE_SPECTRAL_CONSUMABLES |
                             SPECTRAL_TARGETING.keys())
                for i, card in enumerate(consumables):
                    key = card.get("key", "")
                    if key not in all_known:
                        return SellConsumable(
                            i, reason=f"sell unknown consumable: {card.get('label', '?')} (freeing slot)",
                        )
                # Sell stale consumables held 3+ rounds
                for i, card in enumerate(consumables):
                    key = card.get("key", "")
                    rounds_held = self._held_rounds.get(key, 0)
                    if rounds_held >= 3:
                        return SellConsumable(
                            i, reason=f"sell stale consumable: {card.get('label', '?')} (held {rounds_held} rounds)",
                        )
            return None

        # Pick highest use_value
        candidates.sort(key=lambda x: -x[1])
        idx, use_val, label, action = candidates[0]
        # Track last used tarot/planet so Fool knows what it copies
        # Fool only copies Tarots and Planets — not Spectrals
        if isinstance(action, UseConsumable):
            used_card = consumables[idx]
            used_key = used_card.get("key", "")
            used_set = used_card.get("set", "")
            if used_key and used_key != "c_fool" and used_set in ("TAROT", "PLANET"):
                self._last_used_consumable = used_key
        return action

    @staticmethod
    def _action_from_args(action_args: tuple) -> Action:
        """Convert policy action_args tuple into an Action object."""
        kind = action_args[0]
        if kind == "use":
            return UseConsumable(action_args[1], reason=action_args[2])
        elif kind == "use_target":
            return UseConsumable(action_args[1], target_cards=action_args[2], reason=action_args[3])
        elif kind == "rearrange":
            return RearrangeHand(order=action_args[1], reason=action_args[2])
        else:
            raise ValueError(f"Unknown action kind: {kind}")

    @staticmethod
    def _has_edition(joker: dict) -> bool:
        mod = joker.get("modifier", [])
        return isinstance(mod, dict) and bool(mod.get("edition"))

    def _find_hex_target(self, jokers, hand_levels, ante, blind_name: str | None = None):
        """Find the best joker to keep for Hex. Prefer uneditioned jokers."""
        strat = compute_strategy(jokers, hand_levels)
        scored = []
        for j in jokers:
            val = evaluate_joker_value(j, jokers, hand_levels, ante, strat, blind_name=blind_name)
            has_ed = self._has_edition(j)
            scored.append((j, val, has_ed))

        # Prefer uneditioned (Polychrome would be wasted on an already-editioned joker)
        uneditioned = [(j, v) for j, v, ed in scored if not ed]
        if uneditioned:
            uneditioned.sort(key=lambda x: x[1], reverse=True)
            return uneditioned[0][0]

        # All editioned — fall back to best overall (Polychrome overwrites)
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[0][0]

    def _handle_hex_selldown(self, consumables, jokers, hand_levels, ante, blind_name: str | None = None) -> Action | None:
        """Multi-tick sell-down for Hex: sell weak jokers, then use Hex on the survivor."""
        if not self._hex_selling_down:
            return None

        # Verify Hex is still in our consumables
        hex_idx = next(
            (i for i, c in enumerate(consumables) if c.get("key") == "c_hex"), None
        )
        if hex_idx is None:
            self._hex_selling_down = False
            self._hex_target_key = None
            return None

        # Verify target joker is still owned
        target_idx = next(
            (i for i, j in enumerate(jokers) if joker_key(j) == self._hex_target_key), None
        )
        if target_idx is None:
            self._hex_selling_down = False
            self._hex_target_key = None
            return None

        # CRITICAL: never use Hex with 0 jokers (soft-locks the game)
        if len(jokers) == 0:
            self._hex_selling_down = False
            self._hex_target_key = None
            return None

        # Only target joker remains → use Hex
        if len(jokers) == 1:
            self._hex_selling_down = False
            self._hex_target_key = None
            target_label = jokers[0].get("label", "?")
            return UseConsumable(hex_idx, reason=f"use Hex on {target_label} (sell-down complete)")

        # Sell the weakest non-target joker
        strat = compute_strategy(jokers, hand_levels)
        worst_idx = None
        worst_val = float("inf")
        for i, j in enumerate(jokers):
            if joker_key(j) == self._hex_target_key:
                continue
            val = evaluate_joker_value(j, jokers, hand_levels, ante, strat, blind_name=blind_name)
            if val < worst_val:
                worst_val = val
                worst_idx = i

        if worst_idx is None:
            return None

        fodder_label = jokers[worst_idx].get("label", "?")
        remaining = len(jokers) - 2  # after this sell, how many more to go
        target_label = jokers[target_idx].get("label", "?") if target_idx is not None else "?"
        return SellJoker(
            worst_idx,
            reason=f"Hex setup: sell {fodder_label} to isolate {target_label} ({remaining} more to go)",
        )
