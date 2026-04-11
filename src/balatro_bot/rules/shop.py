from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from balatro_bot.actions import (
    BuyCard, BuyPack, BuyVoucher, SellJoker, SellConsumable,
    Reroll, NextRound, RearrangeJokers, Action,
)
from balatro_bot.context import RoundContext
from balatro_bot.constants import (
    SCALING_JOKERS, SCALING_XMULT, PLANET_KEYS, SAFE_CONSUMABLE_TAROTS,
    TARGETING_TAROTS, SAFE_SPECTRAL_CONSUMABLES, SPECTRAL_TARGETING,
)
from balatro_bot.strategy import Strategy, compute_strategy, JOKER_HAND_AFFINITY
from balatro_bot.joker_effects import JOKER_EFFECTS, _noop, parse_effect_value
from balatro_bot.domain.policy.shop_valuation import evaluate_joker_value, UTILITY_VALUE
from balatro_bot.rules._helpers import evaluate_hex

if TYPE_CHECKING:
    from typing import Any

log = logging.getLogger("balatro_bot")

# ---------------------------------------------------------------------------
# Invisible dupe-target scoring constants
# ---------------------------------------------------------------------------
_COPY_JOKER_DUPE_SCORE = 15.0  # copy jokers (Blueprint/Brainstorm) always tier-1
_XMULT_DUPE_MULTIPLIER = 3.0   # xMult value → dupe score (X3 → 9.0, X2 → 6.0)
_MULT_DUPE_DIVISOR = 5.0       # flat mult value → dupe score (+20 → 4.0)
_DEFAULT_DUPE_SCORE = 2.0      # fallback for DUPE_WORTHY jokers without parsed values

# Min dupe-target score that justifies selling down, scales with ante
_MIN_TARGET_BASE = 3.0          # base threshold (ante <= 3)
_MIN_TARGET_SCALE = 1.5         # +1.5 per ante above 3


class SellInvisible:
    """Sell down to best joker + Invisible, then sell Invisible for a guaranteed dupe.

    Invisible duplicates a RANDOM owned joker when sold (after 2+ rounds held).
    To guarantee hitting the best one, we sell all other jokers first until only
    [best, Invisible] remain. Then selling Invisible = 100% dupe.

    Decision gates before starting a sell-down:
    1. Target must be DUPE_WORTHY (xMult, copy jokers, etc.)
    2. Target's score must exceed an ante-scaled threshold — at Ante 3 anything
       decent qualifies, at Ante 7+ only stacked X3+ or Blueprint
    3. Don't start selling if a boss blind is next — beat it first, sell after
    4. Once committed (mid-sequence), always finish
    """
    name = "sell_invisible"

    _DUPE_EXTRA = {
        "j_vampire", "j_hologram", "j_lucky_cat", "j_canio", "j_obelisk",
        "j_yorick", "j_hit_the_road",
        "j_card_sharp", "j_seeing_double",
        "j_blueprint", "j_brainstorm",
    }

    @staticmethod
    def _dupe_worthy() -> set[str]:
        from balatro_bot.domain.policy.shop import ALWAYS_BUY, HIGH_PRIORITY
        return ALWAYS_BUY | HIGH_PRIORITY | SellInvisible._DUPE_EXTRA

    # Copy jokers get a fixed high score — always worth duping
    COPY_JOKERS = {"j_blueprint", "j_brainstorm"}

    def __init__(self) -> None:
        self._first_seen_round: int | None = None
        self._selling_down: bool = False

    def _score_target(self, joker: dict) -> float:
        """Score how valuable this joker is as a dupe target."""
        key = joker.get("key", "")

        if key in self.COPY_JOKERS:
            return _COPY_JOKER_DUPE_SCORE

        effect_text = joker.get("value", {}).get("effect", "")
        parsed = parse_effect_value(effect_text) if effect_text else {}

        if parsed.get("xmult"):
            return parsed["xmult"] * _XMULT_DUPE_MULTIPLIER
        if parsed.get("mult"):
            return parsed["mult"] / _MULT_DUPE_DIVISOR
        return _DEFAULT_DUPE_SCORE

    def _best_dupe_target(self, owned: list[dict], invisible_idx: int) -> tuple[int | None, float]:
        """Return (index, score) of the best dupe target, or (None, 0)."""
        best_idx = None
        best_score = -1.0

        for i, j in enumerate(owned):
            if i == invisible_idx:
                continue
            if j.get("key", "") not in self._dupe_worthy():
                continue
            score = self._score_target(j)
            if score > best_score:
                best_score = score
                best_idx = i

        return best_idx, best_score

    @staticmethod
    def _min_target_score(ante: int) -> float:
        """Minimum target score to justify selling down at this ante."""
        if ante <= 3:
            return _MIN_TARGET_BASE
        return _MIN_TARGET_BASE + (ante - 3) * _MIN_TARGET_SCALE

    @staticmethod
    def _boss_next(round_num: int) -> bool:
        """True if the next blind is a boss (we just beat the big blind)."""
        round_in_ante = ((round_num - 1) % 3) + 1
        return round_in_ante == 2

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        round_num = state.get("round_num", 0)
        ante = state.get("ante_num", 1)
        owned = state.get("jokers", {}).get("cards", [])

        invisible_idx = next(
            (i for i, j in enumerate(owned) if j.get("key") == "j_invisible"), None
        )
        if invisible_idx is None:
            self._first_seen_round = None
            self._selling_down = False
            return None

        if self._first_seen_round is None:
            self._first_seen_round = round_num
            return None

        if round_num - self._first_seen_round < 2:
            return None

        if len(owned) < 2:
            return None

        target_idx, target_score = self._best_dupe_target(owned, invisible_idx)
        if target_idx is None:
            self._selling_down = False
            return None

        # Gate: is the target good enough for this ante?
        threshold = self._min_target_score(ante)
        if target_score < threshold:
            self._selling_down = False
            return None

        # Gate: don't START selling down before a boss blind
        if not self._selling_down and self._boss_next(round_num):
            return None  # beat the boss first, sell after

        target_label = owned[target_idx].get("label", "?")

        # Final step: only [target, Invisible] remain → sell Invisible
        if len(owned) == 2:
            self._selling_down = False
            return SellJoker(
                invisible_idx,
                reason=f"Invisible: guaranteed dupe of {target_label} "
                       f"(score={target_score:.1f}, ante {ante})",
            )

        # Commit to sell-down sequence
        self._selling_down = True

        # Sell the worst non-target, non-Invisible joker
        worst_idx = None
        worst_score = float("inf")
        for i, j in enumerate(owned):
            if i in (invisible_idx, target_idx):
                continue
            sell_val = j.get("cost", {}).get("sell", 0)
            effect_text = j.get("value", {}).get("effect", "")
            parsed = parse_effect_value(effect_text) if effect_text else {}
            score = float(sell_val)
            if parsed.get("xmult") and parsed["xmult"] > 1.0:
                score += parsed["xmult"] * 10
            elif parsed.get("mult") and parsed["mult"] > 0:
                score += parsed["mult"]
            if score < worst_score:
                worst_score = score
                worst_idx = i

        if worst_idx is None:
            return None

        fodder_label = owned[worst_idx].get("label", "?")
        remaining = len(owned) - 2
        return SellJoker(
            worst_idx,
            reason=f"Invisible setup: sell {fodder_label} to isolate {target_label} "
                   f"({remaining} more to go, target score={target_score:.1f})",
        )


class SellDietCola:
    """Sell Diet Cola for a free shop reroll when nothing good is available."""
    name = "sell_diet_cola"

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        from balatro_bot.domain.policy.shop import choose_sell_diet_cola
        return choose_sell_diet_cola(state)


class SellWeakJoker:
    """Sell the weakest joker if slots are full and the shop has a better one."""
    name = "sell_weak_joker"

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        from balatro_bot.domain.policy.shop import choose_sell_weak_joker
        return choose_sell_weak_joker(state)


class FeedCampfire:
    """When Campfire is owned, sell consumables to feed its X0.25 Mult per sell."""
    name = "feed_campfire"

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        from balatro_bot.domain.policy.shop import choose_feed_campfire
        return choose_feed_campfire(state)


class ReorderJokersForScoring:
    """Arrange jokers in optimal scoring order: +chips → +mult → ×mult (left to right).

    Also handles position-dependent jokers:
    - Blueprint: placed immediately left of the best ×mult joker to copy
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
            key = j.get("key", "")
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
            key = j.get("key", "")
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
                available_fodder.sort(key=lambda i: owned[i].get("key", ""))
                sacrifice_idx = available_fodder[0]
                desired_order = [i for i in desired_order if i != sacrifice_idx]
                # Insert Ceremonial after the last +mult joker (or after chips)
                insert_at = 0
                for pos, idx in enumerate(desired_order):
                    if get_joker_phase(owned[idx].get("key", "")) <= PHASE_MULT:
                        insert_at = pos + 1
                desired_order.insert(insert_at, ceremonial_idx)
                desired_order.insert(insert_at + 1, sacrifice_idx)
            else:
                # No fodder — Ceremonial goes rightmost (eats nothing)
                desired_order.append(ceremonial_idx)

        # --- Blueprint: place immediately left of best ×mult joker to copy ---
        if blueprint_idx is not None:
            # Find rightmost ×mult joker position (best copy target)
            best_target_pos = None
            for pos in range(len(desired_order) - 1, -1, -1):
                idx = desired_order[pos]
                key = owned[idx].get("key", "")
                if key in ("j_blueprint", "j_brainstorm"):
                    continue
                phase = get_joker_phase(key)
                if phase == PHASE_XMULT:
                    best_target_pos = pos
                    break
            if best_target_pos is None:
                # No ×mult — find rightmost +mult
                for pos in range(len(desired_order) - 1, -1, -1):
                    idx = desired_order[pos]
                    key = owned[idx].get("key", "")
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
            # Ensure position 0 is a good copyable effect (not noop/brainstorm)
            if desired_order:
                first_key = owned[desired_order[0]].get("key", "")
                first_phase = get_joker_phase(first_key)
                if first_phase == PHASE_NOOP and len(desired_order) > 1:
                    for swap_pos in range(1, len(desired_order)):
                        swap_key = owned[desired_order[swap_pos]].get("key", "")
                        if (get_joker_phase(swap_key) != PHASE_NOOP
                                and swap_key != "j_brainstorm"):
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
        phase_names = {PHASE_NOOP: "noop", PHASE_CHIPS: "+c", PHASE_MULT: "+m", PHASE_XMULT: "×m"}
        order_desc = " ".join(
            f"{owned[i].get('label', '?')}({phase_names.get(get_joker_phase(owned[i].get('key', '')), '?')})"
            for i in desired_order
        )
        return RearrangeJokers(
            order=desired_order,
            reason=f"scoring order: {order_desc}",
        )


class BuyJokersInShop:
    """Buy jokers that improve scoring, respecting interest thresholds."""
    name = "buy_jokers_in_shop"

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        from balatro_bot.domain.policy.shop import choose_buy_joker_in_shop
        return choose_buy_joker_in_shop(state)


class BuyConsumablesInShop:
    """Buy consumables from the shop using dynamic value scoring."""
    name = "buy_consumables_in_shop"

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        from balatro_bot.domain.policy.shop import choose_buy_consumable_in_shop
        return choose_buy_consumable_in_shop(state)


class BuyPacksInShop:
    """Buy packs from the shop, prioritizing Planet > Buffoon > Tarot."""
    name = "buy_packs_in_shop"

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        from balatro_bot.domain.policy.shop import choose_buy_pack_in_shop
        return choose_buy_pack_in_shop(state)


class BuyVouchersInShop:
    """Buy high-impact vouchers when we can afford them."""
    name = "buy_vouchers_in_shop"

    # Voucher keys ranked by priority. Lower = buy first.
    # Only include vouchers with direct gameplay impact.
    VOUCHER_PRIORITY: dict[str, int] = {
        # Tier 1: extra scoring attempts
        "v_grabber": 1,          # +1 hand per round
        "v_nacho_tong": 1,       # +1 more hand
        # Tier 2: bigger hands = better poker hands
        "v_paint_brush": 2,      # +1 hand size
        "v_palette": 2,          # +1 more hand size
        # Tier 3: more discards for hand improvement
        "v_wasteful": 3,         # +1 discard
        "v_recyclomancy": 3,     # +1 more discard
        # Tier 4: more joker capacity
        "v_antimatter": 4,       # +1 joker slot (requires Blank bought)
        # Tier 5: consumable capacity
        "v_crystal_ball": 5,     # +1 consumable slot
        # Tier 6: ante reduction (powerful but trades resources)
        "v_hieroglyph": 6,       # -1 ante, -1 hand per round
        # Tier 7: economy (nice to have)
        "v_seed_money": 7,       # interest cap to $10/round
        "v_money_tree": 7,       # interest cap to $20/round
        "v_clearance_sale": 8,   # 25% off
        "v_overstock": 8,        # +1 shop card slot
    }

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        from balatro_bot.domain.policy.shop import choose_buy_voucher_in_shop
        return choose_buy_voucher_in_shop(state)


class RerollShop:
    """Reroll the shop when we're flush with cash and nothing good is available."""
    name = "reroll_shop"

    # Only reroll above this money threshold (well above interest cap)
    MIN_MONEY_TO_REROLL = 35
    # Reroll costs $5 base (can be reduced by vouchers)
    REROLL_COST = 5
    # Max rerolls per shop visit to prevent infinite loops
    MAX_REROLLS = 3

    def __init__(self) -> None:
        self._rerolls_this_shop = 0
        self._last_round = -1

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        from balatro_bot.domain.policy.shop import choose_reroll_shop

        round_num = state.get("round_num", 0)

        # Reset counter on new shop visit
        if round_num != self._last_round:
            self._rerolls_this_shop = 0
            self._last_round = round_num

        result = choose_reroll_shop(
            state,
            reroll_counter=self._rerolls_this_shop,
            min_money_to_reroll=self.MIN_MONEY_TO_REROLL,
            max_rerolls=self.MAX_REROLLS,
        )
        if result is not None:
            self._rerolls_this_shop += 1
        return result


class LeaveShop:
    """When done shopping, move to next round."""
    name = "leave_shop"

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        from balatro_bot.domain.policy.shop import choose_leave_shop
        return choose_leave_shop()
