from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from balatro_bot.actions import UseConsumable, SellConsumable, SellJoker, RearrangeHand, Action
from balatro_bot.constants import (
    PLANET_KEYS, SAFE_CONSUMABLE_TAROTS, TARGETING_TAROTS,
    IMMEDIATE_TARGETING, TACTICAL_TARGETING,
    SAFE_SPECTRAL_CONSUMABLES, SPECTRAL_TARGETING, SCALING_JOKERS,
    FACE_RANKS_TAROT, NO_TARGET_TAROTS,
)
from balatro_bot.hand_evaluator import best_hand, flush_draw, score_hand
from balatro_bot.strategy import compute_strategy
from balatro_bot.cards import card_suit, card_suits, card_rank, _modifier, rank_value
from balatro_bot.rules._helpers import (
    _find_tarot_targets, _find_gold_targets, _find_enhancement_targets,
    _find_clone_targets, score_consumable, evaluate_hex,
)
from balatro_bot.joker_valuation import evaluate_joker_value

if TYPE_CHECKING:
    from typing import Any
    from balatro_bot.strategy import Strategy

log = logging.getLogger("balatro_bot")


class UseConsumables:
    """Unified consumable usage: scores use_now vs hold_value for each consumable.

    Replaces both UseImmediateConsumables and UseTacticalConsumables.
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
        chips_scored = rnd.get("chips", 0)
        for b in state.get("blinds", {}).values():
            if isinstance(b, dict) and b.get("status") == "CURRENT":
                blind_score = b.get("score", 0)
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
        desperate = hands_left <= 2
        consumable_limit = state.get("consumables", {}).get("limit", 2)
        slots_full = len(consumables) >= consumable_limit

        # Hex sell-down: if mid-sequence, continue selling before anything else
        hex_action = self._handle_hex_selldown(consumables, jokers, hand_levels, ante)
        if hex_action is not None:
            return hex_action

        # Score each consumable: use_value vs hold_value
        candidates: list[tuple[int, float, str, Action]] = []

        for i, card in enumerate(consumables):
            key = card.get("key", "")
            label = card.get("label", "?")

            # --- Special cases that bypass scoring ---

            # Hex: evaluate whether to sell, use, or start sell-down
            if key == "c_hex":
                hex_score = evaluate_hex(jokers, ante, hand_levels)
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
                    target = self._find_hex_target(jokers, hand_levels, ante)
                    self._hex_selling_down = True
                    self._hex_target_key = target.get("key")
                    # Sell the weakest non-target joker this tick
                    return self._handle_hex_selldown(consumables, jokers, hand_levels, ante)

            # --- Compute use_value and hold_value ---
            use_value, action = self._score_use_now(
                i, key, label, card, state, strat, hand_cards, jokers,
                hand_levels, current_best, current_score, chips_remaining,
                can_win_now, desperate, money, discards_left, hands_left,
                joker_limit, ante,
            )
            hold_value = self._score_hold(
                key, ante, slots_full, desperate, hands_left, discards_left,
            )

            # Staleness penalty
            rounds_held = self._held_rounds.get(key, 0)
            if rounds_held >= 2:
                hold_value -= (rounds_held - 1) * 0.5

            if use_value > hold_value and action is not None:
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

    def _score_use_now(
        self, idx: int, key: str, label: str, card: dict,
        state: dict, strat: Strategy, hand_cards: list, jokers: list,
        hand_levels: dict, current_best, current_score: int,
        chips_remaining: int, can_win_now: bool, desperate: bool,
        money: int, discards_left: int, hands_left: int,
        joker_limit: int, ante: int,
    ) -> tuple[float, Action | None]:
        """Return (use_value, action) for using this consumable right now."""

        joker_slots = state.get("jokers", {})

        # --- Planet cards: always use immediately ---
        if key in PLANET_KEYS:
            hand_type = PLANET_KEYS[key]
            affinity = strat.hand_affinity(hand_type) if hand_type != "ALL" else 99
            cur_level = hand_levels.get(hand_type, {}).get("level", 1) if hand_type != "ALL" else 0
            return (10.0, UseConsumable(
                idx, reason=f"[PLANET] {label}: {hand_type} lv{cur_level}→lv{cur_level+1} (affinity={affinity:.0f})",
            ))

        # --- Safe no-target tarots: use immediately ---
        if key in SAFE_CONSUMABLE_TAROTS:
            if key == "c_judgement":
                if joker_slots.get("count", 0) >= joker_slots.get("limit", 5):
                    return (0.0, None)
            if key == "c_wheel_of_fortune":
                if not jokers:
                    return (0.0, None)
                # NOT_ALLOWED if every joker already has an edition
                if all(isinstance(j.get("modifier"), dict) and j["modifier"].get("edition") for j in jokers):
                    return (0.0, None)
            if key == "c_fool":
                if not self._last_used_consumable:
                    return (0.0, None)  # nothing used yet — API will reject
                # Score Fool as whatever it copies
                value = score_consumable(self._last_used_consumable, state, strat)
                return (value, UseConsumable(
                    idx, reason=f"use Fool (copies {self._last_used_consumable}, value={value:.1f})",
                ))
            value = score_consumable(key, state, strat)
            return (value, UseConsumable(idx, reason=f"use tarot: {label} (value={value:.1f})"))

        # --- Safe spectral cards ---
        if key in SAFE_SPECTRAL_CONSUMABLES:
            if key in ("c_ankh", "c_hex") and not jokers:
                return (0.0, None)
            if key == "c_hex":
                hex_val = evaluate_hex(jokers, ante, hand_levels)
                if hex_val <= 0.0:
                    return (0.0, None)
                return (hex_val, UseConsumable(idx, reason=f"use Hex (score={hex_val:.1f})"))
            if key == "c_ankh" and joker_slots.get("count", 0) >= joker_slots.get("limit", 5):
                return (0.0, None)
            if key == "c_wraith" and joker_slots.get("count", 0) >= joker_slots.get("limit", 5):
                return (0.0, None)
            if key == "c_ectoplasm" and ante < 3:
                return (0.0, None)
            return (5.0, UseConsumable(idx, reason=f"use spectral: {label}"))

        # --- Targeting tarots/spectrals ---
        if not hand_cards:
            return (0.0, None)

        # Check Tarots
        if key in TARGETING_TAROTS:
            max_count, effect_type, extra = TARGETING_TAROTS[key]
            return self._score_targeting_use(
                idx, key, label, max_count, effect_type, extra,
                state, strat, hand_cards, jokers, hand_levels,
                current_best, current_score, chips_remaining,
                can_win_now, desperate, money, discards_left, hands_left,
                joker_limit, ante,
            )

        # Check Spectral targeting
        if key in SPECTRAL_TARGETING:
            max_count, effect_type, extra = SPECTRAL_TARGETING[key]
            targets, score = _find_tarot_targets(
                effect_type, extra, max_count, hand_cards, jokers, strat,
                current_best=current_best,
            )
            if targets:
                return (score, UseConsumable(
                    idx, target_cards=targets,
                    reason=f"use spectral: {label} -> targets {targets}",
                ))

        return (0.0, None)

    def _score_targeting_use(
        self, idx: int, key: str, label: str,
        max_count: int, effect_type: str, extra: str | None,
        state: dict, strat: Strategy, hand_cards: list, jokers: list,
        hand_levels: dict, current_best, current_score: int,
        chips_remaining: int, can_win_now: bool, desperate: bool,
        money: int, discards_left: int, hands_left: int,
        joker_limit: int, ante: int,
    ) -> tuple[float, Action | None]:
        """Score and return action for a targeting tarot."""

        # --- Immediate-use targeting (destroy, clone, stone, rank_up) ---
        if effect_type in IMMEDIATE_TARGETING:
            # Death tarot: rearrange hand if needed
            if effect_type == "clone":
                result = _find_clone_targets(hand_cards, strat)
                if result is None:
                    return (0.0, None)
                best_idx, worst_idx = result
                if best_idx > worst_idx:
                    order = list(range(len(hand_cards)))
                    order.remove(best_idx)
                    insert_pos = order.index(worst_idx)
                    order.insert(insert_pos, best_idx)
                    return (4.0, RearrangeHand(
                        order=order,
                        reason=f"rearrange for Death: move {hand_cards[best_idx].get('label','?')} left of {hand_cards[worst_idx].get('label','?')}",
                    ))
                return (4.0, UseConsumable(
                    idx, target_cards=[best_idx, worst_idx],
                    reason=f"Death: clone {hand_cards[best_idx].get('label','?')} onto {hand_cards[worst_idx].get('label','?')}",
                ))

            targets, score = _find_tarot_targets(
                effect_type, extra, max_count, hand_cards, jokers, strat,
                current_best=current_best,
            )
            if targets:
                return (score, UseConsumable(
                    idx, target_cards=targets,
                    reason=f"use tarot: {label} -> targets {targets}",
                ))
            return (0.0, None)

        # --- Tactical targeting (suit_convert, glass, enhance, gold) ---

        # Gold enhancement: fire when round already won
        if effect_type == "gold":
            if can_win_now:
                targets = _find_gold_targets(hand_cards, max_count, current_best)
                if targets:
                    return (4.0, UseConsumable(
                        idx, target_cards=targets,
                        reason=f"gold junk: {label} on held card (round already won)",
                    ))
            return (0.0, None)

        # Suit conversions: fire when it creates a meaningful improvement
        if effect_type == "suit_convert":
            result = self._eval_suit_convert(
                hand_cards, hand_levels, jokers, extra, max_count,
                current_score, strat, money, discards_left, hands_left,
                joker_limit=joker_limit,
            )
            if result:
                new_score, targets = result
                improvement = new_score / max(current_score, 1)
                if improvement > 1.2 or desperate:
                    return (improvement * 3.0, UseConsumable(
                        idx, target_cards=targets,
                        reason=f"tactical: {label} -> Flush ({new_score} vs {current_score}, {hands_left}h left)",
                    ))
            return (0.0, None)

        # Enhancements and Glass: use based on ante timing
        if effect_type in ("glass", "enhance"):
            # Ante-based timing from the plan
            if ante <= 3:
                # Early game: fire freely — deck improvements compound
                threshold = 1.0
            elif ante <= 5:
                # Mid game: need meaningful improvement
                threshold = 1.2
            else:
                # Late game: only when desperate or huge improvement
                if not desperate:
                    return (0.0, None)
                threshold = 1.0

            if effect_type == "glass":
                result = self._eval_glass(hand_cards, hand_levels, jokers, current_best, current_score, strat=strat)
                if result:
                    new_score, targets = result
                    improvement = new_score / max(current_score, 1)
                    if improvement >= threshold or desperate:
                        return (improvement * 3.0, UseConsumable(
                            idx, target_cards=targets,
                            reason=f"{'desperate' if desperate else 'tactical'}: Glass (×{improvement:.1f}, {hands_left}h left)",
                        ))
            else:
                result = self._eval_enhancement(
                    hand_cards, hand_levels, jokers, current_best, current_score,
                    extra, max_count, strat=strat,
                )
                if result:
                    new_score, targets = result
                    improvement = new_score / max(current_score, 1)
                    if improvement >= threshold or desperate:
                        return (improvement * 3.0, UseConsumable(
                            idx, target_cards=targets,
                            reason=f"{'desperate' if desperate else 'tactical'}: {label} ({extra}) (×{improvement:.1f}, {hands_left}h left)",
                        ))

        return (0.0, None)

    def _score_hold(
        self, key: str, ante: int, slots_full: bool,
        desperate: bool, hands_left: int, discards_left: int,
    ) -> float:
        """Score the value of holding this consumable for later."""
        # Planets: always use immediately
        if key in PLANET_KEYS:
            return 0.0
        # No-target tarots: always use immediately
        if key in SAFE_CONSUMABLE_TAROTS:
            return 0.0
        # Spectrals: always use immediately
        if key in SAFE_SPECTRAL_CONSUMABLES:
            return 0.0

        # Tactical consumables: hold value depends on ante timing
        hold = 0.0

        if key in TARGETING_TAROTS:
            _, effect_type, extra = TARGETING_TAROTS[key]

            if effect_type == "suit_convert":
                # Worth holding if we have discards (might draw into flush)
                hold = 2.0 if discards_left > 0 else 0.5

            elif effect_type in ("glass", "enhance"):
                # Early: low hold value (use freely, compound)
                # Late: high hold value (wait for best target)
                if ante <= 3:
                    hold = 1.0
                elif ante <= 5:
                    hold = 2.0
                else:
                    hold = 3.0

            elif effect_type == "gold":
                # Hold until winning
                hold = 2.0

            else:
                # Immediate-use tarots (destroy, clone, etc)
                hold = 0.0

        # Slot pressure: holding a mediocre card blocks better purchases
        if slots_full:
            hold -= 2.0

        return hold

    # --- Evaluation helpers (preserved from UseTacticalConsumables) ---

    def _eval_suit_convert(
        self, hand_cards, hand_levels, jokers, target_suit, max_count,
        current_score, strat, money=0, discards_left=0, hands_left=1,
        joker_limit: int = 5,
    ) -> tuple[int, list[int]] | None:
        """Would converting cards to target_suit create a Flush?"""
        matching = []
        non_matching = []
        for i, c in enumerate(hand_cards):
            suits = card_suits(c)
            if target_suit in suits:
                matching.append(i)
            elif card_suit(c) is not None:
                non_matching.append(i)

        needed = 5 - len(matching)
        if needed <= 0:
            return None
        if needed > min(max_count, len(non_matching)):
            return None

        rank_aff = strat.rank_affinity_dict() if strat else {}
        non_matching.sort(key=lambda i: (
            rank_aff.get(card_rank(hand_cards[i]) or "", 0.0),
            rank_value(card_rank(hand_cards[i]) or "2"),
        ))
        targets = non_matching[:needed]

        flush_cards = matching[:5 - needed]
        flush_cards.extend(targets)
        flush_cards = flush_cards[:5]
        simulated = [hand_cards[i] for i in flush_cards]
        held = [hand_cards[i] for i in range(len(hand_cards)) if i not in set(flush_cards)]
        _, _, flush_score = score_hand(
            "Flush", simulated, hand_levels,
            jokers=jokers, played_cards=simulated, held_cards=held,
            money=money, discards_left=discards_left, hands_left=hands_left,
            joker_limit=joker_limit,
        )

        return (flush_score, targets)

    def _eval_glass(
        self, hand_cards, hand_levels, jokers, current_best, current_score,
        strat: Strategy | None = None,
    ) -> tuple[int, list[int]] | None:
        """Would applying Glass to a card in our best hand boost scoring?"""
        if not current_best:
            return None

        rank_aff = strat.rank_affinity_dict() if strat else {}
        # Score each unenhanced card in the scoring hand
        candidates = []
        for idx in current_best.card_indices:
            c = hand_cards[idx]
            r = card_rank(c)
            if not r or _modifier(c).get("enhancement"):
                continue
            aff = rank_aff.get(r, 0.0)
            is_face = r in FACE_RANKS_TAROT
            # Sort key: high affinity first, then face cards, then high chip value
            candidates.append((idx, -aff, 0 if is_face else 1, -rank_value(r)))

        if not candidates:
            return None

        candidates.sort(key=lambda x: (x[1], x[2], x[3]))
        best_idx = candidates[0][0]
        is_face = candidates[0][2] == 0
        multiplier = 1.8 if is_face else 1.6
        estimated_new = int(current_score * multiplier)
        return (estimated_new, [best_idx])

    def _eval_enhancement(
        self, hand_cards, hand_levels, jokers, current_best, current_score,
        enhancement, max_count, strat: Strategy | None = None,
    ) -> tuple[int, list[int]] | None:
        """Would applying this enhancement boost cards about to score?"""
        if not current_best:
            return None

        rank_aff = strat.rank_affinity_dict() if strat else None

        # Wild (Lovers): check if it enables a Flush
        if enhancement == "Wild":
            fd = flush_draw(hand_cards)
            if fd and len(fd) >= 4:
                for i, c in enumerate(hand_cards):
                    if i not in fd and not _modifier(c).get("enhancement"):
                        estimated = int(current_score * 1.5)
                        return (estimated, [i])

        # Steel: apply to highest-rank held (not played) card
        if enhancement == "Steel":
            scoring_set = set(current_best.card_indices)
            held = [i for i, c in enumerate(hand_cards)
                    if i not in scoring_set and not _modifier(c).get("enhancement")]
            if held:
                RANK_ORDER = ["2", "3", "4", "5", "6", "7", "8", "9", "T", "J", "Q", "K", "A"]
                held.sort(key=lambda i: RANK_ORDER.index(card_rank(hand_cards[i]))
                          if card_rank(hand_cards[i]) in RANK_ORDER else -1, reverse=True)
                estimated = int(current_score * 1.5)
                return (estimated, held[:max_count])
            return None

        # Other enhancements: apply to highest-value scoring card, prefer affinity ranks
        targets = _find_enhancement_targets(hand_cards, max_count, rank_affinity=rank_aff)
        if targets:
            scoring_set = set(current_best.card_indices)
            relevant = [t for t in targets if t in scoring_set]
            if relevant:
                estimated = int(current_score * 1.2)
                return (estimated, relevant[:max_count])

        return None

    @staticmethod
    def _has_edition(joker: dict) -> bool:
        mod = joker.get("modifier", [])
        return isinstance(mod, dict) and bool(mod.get("edition"))

    def _find_hex_target(self, jokers, hand_levels, ante):
        """Find the best joker to keep for Hex. Prefer uneditioned jokers."""
        strat = compute_strategy(jokers, hand_levels)
        scored = []
        for j in jokers:
            val = evaluate_joker_value(j, jokers, hand_levels, ante, strat)
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

    def _handle_hex_selldown(self, consumables, jokers, hand_levels, ante) -> Action | None:
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
            (i for i, j in enumerate(jokers) if j.get("key") == self._hex_target_key), None
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
            if j.get("key") == self._hex_target_key:
                continue
            val = evaluate_joker_value(j, jokers, hand_levels, ante, strat)
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
