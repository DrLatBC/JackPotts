from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from balatro_bot.actions import UseConsumable, SellConsumable, RearrangeHand, Action
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
    _find_clone_targets, score_consumable,
)

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

        # Score each consumable: use_value vs hold_value
        candidates: list[tuple[int, float, str, Action]] = []

        for i, card in enumerate(consumables):
            key = card.get("key", "")
            label = card.get("label", "?")

            # --- Special cases that bypass scoring ---

            # Hex: sell if joker lineup established
            if key == "c_hex":
                joker_count = joker_slots.get("count", 0)
                owned_keys = {j.get("key") for j in jokers}
                if joker_count >= joker_limit or owned_keys & SCALING_JOKERS or ante >= 5:
                    reason = "full slots" if joker_count >= joker_limit else (
                        "scaling joker" if owned_keys & SCALING_JOKERS else f"ante {ante}"
                    )
                    return SellConsumable(i, reason=f"sell Hex (would destroy joker lineup: {reason})")

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
                if joker_slots.get("count", 0) == 0:
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
                result = self._eval_glass(hand_cards, hand_levels, jokers, current_best, current_score)
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
                    extra, max_count,
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
    ) -> tuple[int, list[int]] | None:
        """Would applying Glass to a face card in our best hand boost scoring?"""
        if not current_best:
            return None

        for idx in current_best.card_indices:
            c = hand_cards[idx]
            r = card_rank(c)
            if r in FACE_RANKS_TAROT and not _modifier(c).get("enhancement"):
                estimated_new = int(current_score * 1.8)
                return (estimated_new, [idx])

        # Also check non-face cards if no face cards available
        for idx in current_best.card_indices:
            c = hand_cards[idx]
            r = card_rank(c)
            if r and not _modifier(c).get("enhancement"):
                estimated_new = int(current_score * 1.6)
                return (estimated_new, [idx])

        return None

    def _eval_enhancement(
        self, hand_cards, hand_levels, jokers, current_best, current_score,
        enhancement, max_count,
    ) -> tuple[int, list[int]] | None:
        """Would applying this enhancement boost cards about to score?"""
        if not current_best:
            return None

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

        # Other enhancements: apply to highest-value scoring card
        targets = _find_enhancement_targets(hand_cards, max_count)
        if targets:
            scoring_set = set(current_best.card_indices)
            relevant = [t for t in targets if t in scoring_set]
            if relevant:
                estimated = int(current_score * 1.2)
                return (estimated, relevant[:max_count])

        return None


# Keep old names as aliases for backwards compatibility during transition
UseImmediateConsumables = UseConsumables
UseTacticalConsumables = UseConsumables
