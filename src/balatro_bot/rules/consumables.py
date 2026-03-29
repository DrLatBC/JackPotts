from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from balatro_bot.actions import UseConsumable, SellConsumable, RearrangeHand, Action
from balatro_bot.constants import (
    PLANET_KEYS, SAFE_CONSUMABLE_TAROTS, TARGETING_TAROTS,
    IMMEDIATE_TARGETING, TACTICAL_TARGETING,
    SAFE_SPECTRAL_CONSUMABLES, SPECTRAL_TARGETING, SCALING_JOKERS,
    FACE_RANKS_TAROT,
)
from balatro_bot.hand_evaluator import best_hand, flush_draw, score_hand
from balatro_bot.strategy import compute_strategy
from balatro_bot.cards import card_suit, card_suits, card_rank, _modifier, rank_value
from balatro_bot.rules._helpers import (
    _find_tarot_targets, _find_gold_targets, _find_enhancement_targets,
)

if TYPE_CHECKING:
    from typing import Any

log = logging.getLogger("balatro_bot")


class UseImmediateConsumables:
    """Use planets, no-target tarots, and permanent deck-change tarots immediately."""
    name = "use_immediate_consumables"

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        consumables = state.get("consumables", {}).get("cards", [])
        if not consumables:
            return None

        hand_cards = state.get("hand", {}).get("cards", [])
        jokers = state.get("jokers", {}).get("cards", [])
        hand_levels = state.get("hands", {})
        joker_slots = state.get("jokers", {})
        ante = state.get("ante_num", 1)

        # Priority 1: Use planet cards immediately
        strat = compute_strategy(jokers, hand_levels)
        for i, card in enumerate(consumables):
            key = card.get("key", "")
            if key in PLANET_KEYS:
                hand_type = PLANET_KEYS[key]
                affinity = strat.hand_affinity(hand_type) if hand_type != "ALL" else 99
                cur_level = hand_levels.get(hand_type, {}).get("level", 1) if hand_type != "ALL" else 0
                return UseConsumable(
                    i, reason=f"[PLANET] {card.get('label', '?')}: {hand_type} lv{cur_level}→lv{cur_level+1} (affinity={affinity:.0f})",
                )

        # Priority 2: Use safe no-target Tarots
        for i, card in enumerate(consumables):
            key = card.get("key", "")
            if key in SAFE_CONSUMABLE_TAROTS:
                if key == "c_judgement":
                    if joker_slots.get("count", 0) >= joker_slots.get("limit", 5):
                        continue
                if key == "c_wheel_of_fortune":
                    if joker_slots.get("count", 0) == 0:
                        continue  # needs at least 1 joker to add edition to
                return UseConsumable(
                    i, reason=f"use tarot: {card.get('label', '?')}",
                )

        # Priority 2.5: Use safe no-target Spectral cards (with per-card conditions)
        for i, card in enumerate(consumables):
            key = card.get("key", "")
            if key not in SAFE_SPECTRAL_CONSUMABLES:
                continue
            if key in ("c_ankh", "c_hex") and not jokers:
                continue  # nothing to clone/buff
            if key == "c_hex":
                # Hex destroys ALL jokers except 1 — sell it when established
                joker_count = joker_slots.get("count", 0)
                joker_limit = joker_slots.get("limit", 5)
                owned_keys = {j.get("key") for j in jokers}
                if joker_count >= joker_limit or owned_keys & SCALING_JOKERS or ante >= 5:
                    reason = "full slots" if joker_count >= joker_limit else (
                        "scaling joker" if owned_keys & SCALING_JOKERS else f"ante {ante}"
                    )
                    return SellConsumable(i, reason=f"sell Hex (would destroy joker lineup: {reason})")

            if key == "c_ankh":
                if joker_slots.get("count", 0) >= joker_slots.get("limit", 5):
                    continue  # no slot for the cloned joker
            if key == "c_wraith":
                if joker_slots.get("count", 0) >= joker_slots.get("limit", 5):
                    continue  # no slot for the rare joker
            if key == "c_ectoplasm" and ante < 3:
                continue  # -1 hand size too costly early game
            return UseConsumable(
                i, reason=f"use spectral: {card.get('label', '?')}",
            )

        # Priority 3: Use permanent targeting Tarots and Spectrals
        if hand_cards:
            strat = compute_strategy(jokers, hand_levels)
            money = state.get("money", 0)
            rnd = state.get("round", {})
            discards_left = rnd.get("discards_left", 0)
            hands_left = rnd.get("hands_left", 1)
            joker_limit = state.get("jokers", {}).get("limit", 5)
            current_best = best_hand(hand_cards, hand_levels, jokers=jokers,
                                     money=money, discards_left=discards_left, hands_left=hands_left,
                                     joker_limit=joker_limit)

            # Check Tarots first
            for i, card in enumerate(consumables):
                key = card.get("key", "")
                if key not in TARGETING_TAROTS:
                    continue
                max_count, effect_type, extra = TARGETING_TAROTS[key]
                if effect_type not in IMMEDIATE_TARGETING:
                    continue

                # Death tarot: rearrange hand if needed so best is left of worst
                if effect_type == "clone":
                    from balatro_bot.rules._helpers import _find_clone_targets
                    result = _find_clone_targets(hand_cards, strat)
                    if result is None:
                        continue
                    best_idx, worst_idx = result
                    if best_idx > worst_idx:
                        order = list(range(len(hand_cards)))
                        order.remove(best_idx)
                        insert_pos = order.index(worst_idx)
                        order.insert(insert_pos, best_idx)
                        return RearrangeHand(
                            order=order,
                            reason=f"rearrange for Death: move {hand_cards[best_idx].get('label','?')} left of {hand_cards[worst_idx].get('label','?')}",
                        )
                    return UseConsumable(
                        i, target_cards=[best_idx, worst_idx],
                        reason=f"Death: clone {hand_cards[best_idx].get('label','?')} onto {hand_cards[worst_idx].get('label','?')}",
                    )

                targets, score = _find_tarot_targets(
                    effect_type, extra, max_count, hand_cards, jokers, strat,
                    current_best=current_best,
                )
                if targets:
                    return UseConsumable(
                        i, target_cards=targets,
                        reason=f"use tarot: {card.get('label', '?')} -> targets {targets}",
                    )

            # Check Spectral targeting cards
            for i, card in enumerate(consumables):
                key = card.get("key", "")
                if key not in SPECTRAL_TARGETING:
                    continue
                max_count, effect_type, extra = SPECTRAL_TARGETING[key]
                targets, score = _find_tarot_targets(
                    effect_type, extra, max_count, hand_cards, jokers, strat,
                    current_best=current_best,
                )
                if targets:
                    return UseConsumable(
                        i, target_cards=targets,
                        reason=f"use spectral: {card.get('label', '?')} -> targets {targets}",
                    )

        # Priority 4: Sell unknown consumables to free slots
        all_known = PLANET_KEYS.keys() | SAFE_CONSUMABLE_TAROTS | TARGETING_TAROTS.keys() | SAFE_SPECTRAL_CONSUMABLES | SPECTRAL_TARGETING.keys()
        consumable_limit = state.get("consumables", {}).get("limit", 2)
        if len(consumables) >= consumable_limit:
            for i, card in enumerate(consumables):
                key = card.get("key", "")
                if key not in all_known:
                    return SellConsumable(
                        i, reason=f"sell unknown consumable: {card.get('label', '?')} (freeing slot)",
                    )

        return None


class UseTacticalConsumables:
    """Use suit conversions, Glass, and enhancements tactically based on current hand.

    Evaluates whether using a Tarot would create a significantly better hand
    than what we currently have (e.g., converting off-suit cards to create a Flush).
    """
    name = "use_tactical_consumables"

    # Minimum score improvement to justify using a consumable
    MIN_IMPROVEMENT = 1.3  # new hand must be 30% better

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        consumables = state.get("consumables", {}).get("cards", [])
        if not consumables:
            return None

        hand_cards = state.get("hand", {}).get("cards", [])
        if not hand_cards:
            return None

        jokers = state.get("jokers", {}).get("cards", [])
        hand_levels = state.get("hands", {})

        # Find tactical consumables
        tactical = []
        for i, card in enumerate(consumables):
            key = card.get("key", "")
            if key in TARGETING_TAROTS:
                max_count, effect_type, extra = TARGETING_TAROTS[key]
                if effect_type in TACTICAL_TARGETING:
                    tactical.append((i, key, max_count, effect_type, extra))

        if not tactical:
            return None

        strat = compute_strategy(jokers, hand_levels)
        money = state.get("money", 0)
        rnd = state.get("round", {})
        discards_left = rnd.get("discards_left", 0)
        hands_left = rnd.get("hands_left", 1)

        # Blind progress — how many chips still needed
        blind_score = 0
        chips_scored = rnd.get("chips", 0)
        for b in state.get("blinds", {}).values():
            if isinstance(b, dict) and b.get("status") == "CURRENT":
                blind_score = b.get("score", 0)
                break
        chips_remaining = max(0, blind_score - chips_scored)

        # Desperation: last 2 hands and current score won't clear the blind
        desperate = hands_left <= 2

        joker_limit = state.get("jokers", {}).get("limit", 5)
        current_best = best_hand(hand_cards, hand_levels, jokers=jokers,
                                 money=money, discards_left=discards_left, hands_left=hands_left,
                                 joker_limit=joker_limit)
        current_score = current_best.total if current_best else 0

        # If we can already beat the blind with chips in hand, don't burn consumables
        # unless we're on the last hand (use it or lose it)
        can_win_now = current_score >= chips_remaining

        # Gold enhancement: fire NOW when we know we're winning — the target card
        # will sit in hand collecting $3/round. Only when the round is already won.
        if can_win_now:
            for cons_idx, key, max_count, effect_type, extra in tactical:
                if effect_type != "gold":
                    continue
                targets = _find_gold_targets(hand_cards, max_count, current_best)
                if targets:
                    label = consumables[cons_idx].get("label", "?")
                    return UseConsumable(
                        cons_idx, target_cards=targets,
                        reason=f"gold junk: {label} on held card (round already won)",
                    )

        if can_win_now and hands_left > 1:
            return None

        best_action = None
        best_new_score = current_score

        for cons_idx, key, max_count, effect_type, extra in tactical:
            label = consumables[cons_idx].get("label", "?")

            if effect_type == "suit_convert":
                # Suit conversions are time-sensitive: the hand setup (4 of a suit)
                # may not recur. Use whenever it creates a Flush improvement.
                result = self._eval_suit_convert(
                    hand_cards, hand_levels, jokers, extra, max_count,
                    current_score, strat, money, discards_left, hands_left,
                    joker_limit=joker_limit,
                )
                if result and result[0] > best_new_score:
                    best_new_score, targets = result
                    best_action = UseConsumable(
                        cons_idx, target_cards=targets,
                        reason=f"tactical: {label} -> Flush ({best_new_score} vs {current_score}, {hands_left}h left)",
                    )

            elif effect_type in ("glass", "enhance"):
                # Permanent enhancements: save until desperate or last hand.
                # They improve the deck long-term, so don't burn early.
                if not desperate:
                    continue

                if effect_type == "glass":
                    result = self._eval_glass(
                        hand_cards, hand_levels, jokers, current_best, current_score,
                    )
                    if result and result[0] > best_new_score:
                        best_new_score, targets = result
                        best_action = UseConsumable(
                            cons_idx, target_cards=targets,
                            reason=f"desperate: Glass on face card ({best_new_score} vs {current_score}, {hands_left}h left)",
                        )
                else:
                    enhancement = extra
                    result = self._eval_enhancement(
                        hand_cards, hand_levels, jokers, current_best, current_score,
                        enhancement, max_count,
                    )
                    if result and result[0] > best_new_score:
                        best_new_score, targets = result
                        best_action = UseConsumable(
                            cons_idx, target_cards=targets,
                            reason=f"desperate: {label} ({enhancement}) ({best_new_score} vs {current_score}, {hands_left}h left)",
                        )

        # Suit conversions still need to clear the improvement bar.
        # Desperate enhancements just need to be better than current.
        if best_action and best_new_score > current_score * self.MIN_IMPROVEMENT:
            return best_action

        return None

    def _eval_suit_convert(
        self, hand_cards, hand_levels, jokers, target_suit, max_count,
        current_score, strat, money=0, discards_left=0, hands_left=1,
        joker_limit: int = 5,
    ) -> tuple[int, list[int]] | None:
        """Would converting cards to target_suit create a Flush?"""
        # Count how many cards already match the target suit
        matching = []
        non_matching = []
        for i, c in enumerate(hand_cards):
            suits = card_suits(c)
            if target_suit in suits:
                matching.append(i)
            elif card_suit(c) is not None:
                non_matching.append(i)

        # Need at least 3 matching + enough convertible to reach 5
        needed = 5 - len(matching)
        if needed <= 0:
            return None  # already have a flush
        if needed > min(max_count, len(non_matching)):
            return None  # can't convert enough

        # Pick low-affinity, low-value non-matching cards to convert
        # (protect high-affinity ranks even if they're the wrong suit)
        rank_aff = strat.rank_affinity_dict() if strat else {}
        non_matching.sort(key=lambda i: (
            rank_aff.get(card_rank(hand_cards[i]) or "", 0.0),
            rank_value(card_rank(hand_cards[i]) or "2"),
        ))
        targets = non_matching[:needed]

        # Simulate: what would the Flush score?
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

        # Find face cards in the best hand's scoring cards that aren't enhanced
        for idx in current_best.card_indices:
            c = hand_cards[idx]
            r = card_rank(c)
            if r in FACE_RANKS_TAROT and not _modifier(c).get("enhancement"):
                # Glass gives ×2 mult when scored — rough estimate: double the score
                estimated_new = int(current_score * 1.8)  # conservative estimate
                return (estimated_new, [idx])

        return None

    def _eval_enhancement(
        self, hand_cards, hand_levels, jokers, current_best, current_score,
        enhancement, max_count,
    ) -> tuple[int, list[int]] | None:
        """Would applying this enhancement boost cards about to score?"""
        if not current_best:
            return None

        # Wild (Lovers) is special — check if it would enable a Flush
        if enhancement == "Wild":
            # A Wild card counts as ALL suits. Check if we're 1 card away from Flush
            fd = flush_draw(hand_cards)
            if fd and len(fd) >= 4:
                # Find a non-matching card in hand that isn't already Wild
                for i, c in enumerate(hand_cards):
                    if i not in fd and not _modifier(c).get("enhancement"):
                        # Making this Wild would complete the flush possibility
                        estimated = int(current_score * 1.5)
                        return (estimated, [i])

        # Steel — value comes from cards held in hand (NOT played). Apply to highest-rank
        # unenhanced card that will stay in hand this round, so it contributes ×1.5 mult NOW.
        if enhancement == "Steel":
            scoring_set = set(current_best.card_indices)
            held = [i for i, c in enumerate(hand_cards)
                    if i not in scoring_set and not _modifier(c).get("enhancement")]
            if held:
                RANK_ORDER = ["2","3","4","5","6","7","8","9","T","J","Q","K","A"]
                held.sort(key=lambda i: RANK_ORDER.index(card_rank(hand_cards[i]))
                          if card_rank(hand_cards[i]) in RANK_ORDER else -1, reverse=True)
                estimated = int(current_score * 1.5)  # ×1.5 mult per Steel held
                return (estimated, held[:max_count])
            return None

        # For other enhancements, apply to highest-value non-enhanced scoring card
        targets = _find_enhancement_targets(hand_cards, max_count)
        if targets:
            # Check if target is in the current best hand (about to be played)
            scoring_set = set(current_best.card_indices)
            relevant = [t for t in targets if t in scoring_set]
            if relevant:
                estimated = int(current_score * 1.2)  # modest boost
                return (estimated, relevant[:max_count])

        return None
