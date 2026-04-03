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
from balatro_bot.joker_valuation import evaluate_joker_value, UTILITY_VALUE
from balatro_bot.rules._helpers import evaluate_hex

if TYPE_CHECKING:
    from typing import Any

log = logging.getLogger("balatro_bot")

# --- Module-level priority sets (used by both SellWeakJoker and BuyJokersInShop) ---

# Jokers worth force-buying at any ante — instant xMult or strong unconditional value
_ALWAYS_BUY = {
    "j_cavendish", "j_stencil",
    "j_duo", "j_trio", "j_family", "j_order", "j_tribe",
    "j_gros_michel", "j_popcorn",
    "j_acrobat", "j_blackboard", "j_flower_pot",
    "j_madness",
}

# Scaling xMult jokers — force-buy with relaxed ante gate (ante <= 5)
_HIGH_PRIORITY = {
    "j_constellation",  # +X0.1 per planet used
    "j_campfire",       # +X0.25 per sell, resets at boss
}

# xMult jokers worth aggressively selling flat jokers to acquire
_HIGH_VALUE_XMULT = {
    "j_cavendish", "j_stencil",
    "j_duo", "j_trio", "j_family", "j_order", "j_tribe",
    "j_acrobat", "j_blackboard", "j_flower_pot",
    "j_madness",
    "j_constellation", "j_campfire",
}

# --- Utility joker values (non-scoring jokers with gameplay impact) ---


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

    DUPE_WORTHY = _ALWAYS_BUY | _HIGH_PRIORITY | {
        "j_vampire", "j_hologram", "j_lucky_cat", "j_canio", "j_obelisk",
        "j_yorick", "j_hit_the_road",
        "j_card_sharp", "j_seeing_double",
        "j_blueprint", "j_brainstorm",
    }

    # Copy jokers get a fixed high score — always worth duping
    COPY_JOKERS = {"j_blueprint", "j_brainstorm"}

    def __init__(self) -> None:
        self._first_seen_round: int | None = None
        self._selling_down: bool = False

    def _score_target(self, joker: dict) -> float:
        """Score how valuable this joker is as a dupe target."""
        key = joker.get("key", "")

        if key in self.COPY_JOKERS:
            return 15.0  # copy jokers are always tier-1

        effect_text = joker.get("value", {}).get("effect", "")
        parsed = parse_effect_value(effect_text) if effect_text else {}

        if parsed.get("xmult"):
            return parsed["xmult"] * 3.0  # X3 → 9.0, X2 → 6.0
        if parsed.get("mult"):
            return parsed["mult"] / 5.0
        return 2.0  # default for DUPE_WORTHY without parsed values

    def _best_dupe_target(self, owned: list[dict], invisible_idx: int) -> tuple[int | None, float]:
        """Return (index, score) of the best dupe target, or (None, 0)."""
        best_idx = None
        best_score = -1.0

        for i, j in enumerate(owned):
            if i == invisible_idx:
                continue
            if j.get("key", "") not in self.DUPE_WORTHY:
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
            return 3.0
        return 3.0 + (ante - 3) * 1.5

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
    """Sell Diet Cola for a free shop reroll when nothing good is available.

    Diet Cola gives a free reroll when sold. Worth using when the current
    shop has no high-priority jokers and we have open joker slots.
    """
    name = "sell_diet_cola"

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        owned = state.get("jokers", {}).get("cards", [])

        diet_idx = next(
            (i for i, j in enumerate(owned) if j.get("key") == "j_diet_cola"), None
        )
        if diet_idx is None:
            return None

        # Don't sell if shop already has a good joker
        shop = state.get("shop", {})
        for card in shop.get("cards", []):
            if card.get("set") == "JOKER":
                key = card.get("key", "")
                if key in _ALWAYS_BUY or key in _HIGH_PRIORITY:
                    return None  # good joker available, buy it instead

        return SellJoker(diet_idx, reason="Diet Cola: sell for free shop reroll")


class SellWeakJoker:
    """Sell the weakest joker if slots are full and the shop has a better one."""
    name = "sell_weak_joker"

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        joker_info = state.get("jokers", {})
        owned = joker_info.get("cards", [])

        # Proactive sell: Popcorn decayed to ≤4 mult — cash out before it disappears
        for i, j in enumerate(owned):
            if j.get("key") == "j_popcorn":
                effect_text = j.get("value", {}).get("effect", "")
                parsed = parse_effect_value(effect_text) if effect_text else {}
                current_mult = parsed.get("mult", 99)
                if current_mult <= 4:
                    return SellJoker(
                        i, reason=f"sell decayed Popcorn (+{current_mult} Mult, about to disappear)"
                    )

        # Proactive sell: Ramen decayed below X1.0 — it's now REDUCING scores
        for i, j in enumerate(owned):
            if j.get("key") == "j_ramen":
                effect_text = j.get("value", {}).get("effect", "")
                parsed = parse_effect_value(effect_text) if effect_text else {}
                current_xmult = parsed.get("xmult")
                if current_xmult is not None and current_xmult < 1.0:
                    return SellJoker(
                        i, reason=f"sell decayed Ramen (X{current_xmult:.2f}, reducing scores)"
                    )

        # Only consider selling when slots are full
        if joker_info.get("count", 0) < joker_info.get("limit", 5):
            return None

        if not owned:
            return None

        hand_levels = state.get("hands", {})
        strat = compute_strategy(owned, hand_levels)

        # Don't sell if we have no strategic direction yet
        if not strat.preferred_hands:
            return None

        # Check if the shop has a high-tier xMult joker — if so, we're more
        # willing to sell flat jokers (including normally-protected flat scalers)
        shop = state.get("shop", {})
        ante = state.get("ante_num", 1)
        shop_has_xmult_buy = any(
            card.get("key") in _HIGH_VALUE_XMULT
            for card in shop.get("cards", [])
            if card.get("set") == "JOKER"
        )

        # Score each owned joker — never sell Madness or Ceremonial.
        # When a high-tier xMult is in the shop, allow selling flat scaling
        # jokers (chips/mult scalers) that are normally protected.
        # Stale scalers (barely accumulated value by mid-game) lose protection.
        always_protected = {"j_madness", "j_ceremonial"} | SCALING_XMULT
        if shop_has_xmult_buy:
            protected = always_protected  # flat scalers become sellable
        else:
            protected = always_protected | SCALING_JOKERS

        def _is_stale_scaler(j: dict, cur_ante: int) -> bool:
            """Check if a scaling joker has barely accumulated value for its ante."""
            key = j.get("key", "")
            if key not in SCALING_JOKERS or key in always_protected:
                return False
            effect_text = j.get("value", {}).get("effect", "")
            parsed = parse_effect_value(effect_text) if effect_text else {}
            chips = parsed.get("chips") or 0
            mult = parsed.get("mult") or 0
            if cur_ante >= 4 and chips <= 20 and mult <= 5:
                return True
            if cur_ante >= 6 and chips <= 50 and mult <= 10:
                return True
            return False

        owned_values = [
            (i, evaluate_joker_value(j, owned_jokers=owned,
                                     hand_levels=hand_levels, ante=ante, strategy=strat), j)
            for i, j in enumerate(owned)
            if j.get("key") not in protected or _is_stale_scaler(j, ante)
        ]
        if not owned_values:
            return None

        # Find the weakest joker
        weakest_idx, weakest_value, weakest_joker = min(owned_values, key=lambda x: x[1])

        # Check if the shop has a better joker available
        money = state.get("money", 0)
        sell_value = weakest_joker.get("cost", {}).get("sell", 0)

        INTEREST_CAP = 25
        current_interest = min(money // 5, 5)

        # Score ALL shop jokers and pick the best upgrade
        best_sell_target = None
        best_shop_value = -1.0
        best_shop_label = ""
        best_threshold = 0.0

        weakest_key = weakest_joker.get("key", "")
        weakest_on_strategy = any(
            strat.hand_affinity(ht) > 0
            for ht in JOKER_HAND_AFFINITY.get(weakest_key, ([], 0))[0]
        )

        for card in shop.get("cards", []):
            if card.get("set") != "JOKER":
                continue
            cost = card.get("cost", {}).get("buy", 999)
            money_after_sell = money + sell_value
            if cost > money_after_sell:
                continue

            if ante < 5 and money_after_sell < INTEREST_CAP:
                interest_after_buy = min((money_after_sell - cost) // 5, 5)
                if interest_after_buy < current_interest:
                    continue

            shop_value = evaluate_joker_value(card, owned_jokers=owned,
                                              hand_levels=hand_levels, ante=ante, strategy=strat)
            shop_key = card.get("key", "")
            is_high_tier_xmult = shop_key in _HIGH_VALUE_XMULT

            shop_on_strategy = any(
                strat.hand_affinity(ht) > 0
                for ht in JOKER_HAND_AFFINITY.get(shop_key, ([], 0))[0]
            )

            if is_high_tier_xmult:
                threshold = 0.5
            elif weakest_on_strategy and not shop_on_strategy:
                threshold = 3.0
            elif weakest_on_strategy and shop_on_strategy:
                threshold = 1.5
            else:
                threshold = 1.0

            if shop_value > weakest_value + threshold and shop_value > best_shop_value:
                best_sell_target = card
                best_shop_value = shop_value
                best_shop_label = card.get("label", "?")
                best_threshold = threshold

        if best_sell_target is not None:
            return SellJoker(
                weakest_idx,
                reason=f"sell {weakest_joker.get('label', '?')} (value={weakest_value:.1f}) "
                       f"for {best_shop_label} (value={best_shop_value:.1f}, threshold={best_threshold:.1f}) "
                       f"[strategy: {', '.join(n for n, _ in strat.preferred_hands[:2])}]",
            )

        return None


class FeedCampfire:
    """When Campfire is owned, sell consumables that feed its X0.25 Mult per sell.

    Campfire gains X0.25 Mult each time a joker, tarot, or planet is sold
    (deliberate sell only — destruction doesn't count). Resets at each boss blind.

    Sells:
    - Planets that level hands with no strategy affinity (we'd never play them)
    - Unrecognized/unusable consumables sitting in our slots

    Does NOT sell:
    - Black Hole (levels everything — always use)
    - Planets for our strategy hands (use them to level up)
    - Tarots/Spectrals we know how to use
    """
    name = "feed_campfire"

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        owned_jokers = state.get("jokers", {}).get("cards", [])
        if not any(j.get("key") == "j_campfire" for j in owned_jokers):
            return None

        consumables = state.get("consumables", {}).get("cards", [])
        if not consumables:
            return None

        hand_levels = state.get("hands", {})
        strat = compute_strategy(owned_jokers, hand_levels)

        all_useful = (
            SAFE_CONSUMABLE_TAROTS
            | set(TARGETING_TAROTS)
            | SAFE_SPECTRAL_CONSUMABLES
            | set(SPECTRAL_TARGETING)
        )

        for i, card in enumerate(consumables):
            key = card.get("key", "")

            # Planets: keep Black Hole and any that level a strategy hand
            if key in PLANET_KEYS:
                hand_type = PLANET_KEYS[key]
                if hand_type == "ALL":
                    continue  # Black Hole — always use, never sell
                if strat.hand_affinity(hand_type) > 0:
                    continue  # Levels a hand we care about — use it
                return SellConsumable(
                    i,
                    reason=f"Campfire: sell {card.get('label', '?')} (+X0.25 Mult, {hand_type} has no affinity)",
                )

            # Hex: sell if not worth using (nuanced evaluation)
            if key == "c_hex":
                ante = state.get("ante_num", 1)
                hand_levels = state.get("hands", {})
                hex_score = evaluate_hex(owned_jokers, ante, hand_levels)
                if hex_score <= 0.0:
                    return SellConsumable(
                        i, reason="Campfire: sell Hex (+X0.25 Mult, not worth using)",
                    )

            # Tarots/Spectrals we know how to use — keep them
            if key in all_useful:
                continue

            # Unknown or unusable consumable — sell for Campfire
            return SellConsumable(
                i,
                reason=f"Campfire: sell {card.get('label', '?')} (+X0.25 Mult)",
            )

        return None


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
        from balatro_bot.joker_scoring_phase import (
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

    INTEREST_CAP = 25
    # Minimum valuation score to justify a purchase that loses interest
    MIN_VALUE = 1.5

    ALWAYS_BUY = _ALWAYS_BUY
    HIGH_PRIORITY = _HIGH_PRIORITY

    def _interest_after(self, money: int, cost: int) -> int:
        return min((money - cost) // 5, 5)

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        money = state.get("money", 0)
        shop = state.get("shop", {})
        joker_slots = state.get("jokers", {})
        ante = state.get("ante_num", 1)

        if joker_slots.get("count", 0) >= joker_slots.get("limit", 5):
            # Log interesting jokers we can't buy because slots are full
            for card in shop.get("cards", []):
                if card.get("set") != "JOKER":
                    continue
                key = card.get("key", "")
                if key in self.ALWAYS_BUY or key in self.HIGH_PRIORITY:
                    label = card.get("label", "?")
                    cost = card.get("cost", {}).get("buy", 999)
                    log.info("[SHOP] %s($%d): slots full (%d/%d) — can't buy",
                             label, cost, joker_slots.get("count", 0), joker_slots.get("limit", 5))
            return None

        current_interest = min(money // 5, 5)

        strat = compute_strategy(joker_slots.get("cards", []), state.get("hands", {}))
        jlimit = joker_slots.get("limit", 5)

        # Score each candidate and pick the best improvement
        best_idx = None
        best_improvement = 0.0
        best_cost = 0
        best_label = ""
        passed_on: list[str] = []

        for i, card in enumerate(shop.get("cards", [])):
            if card.get("set") != "JOKER":
                continue
            label = card.get("label", "?")
            cost = card.get("cost", {}).get("buy", 999)
            if cost > money:
                passed_on.append(f"{label}(${cost}, can't afford)")
                continue

            key = card.get("key", "")
            joker_count = joker_slots.get("count", 0)
            owned_keys = {j.get("key") for j in joker_slots.get("cards", [])}

            # Anti-synergy: don't buy jokers that conflict with owned jokers
            from balatro_bot.scaling import check_anti_synergy
            blocker = check_anti_synergy(key, owned_keys)
            if blocker:
                passed_on.append(f"{label}(${cost}, conflicts with {blocker})")
                continue

            # S-tier jokers: buy immediately, ignore interest thresholds
            # Exception: don't buy Madness if we own a scaling joker it could eat
            if key == "j_madness":
                owned_keys = {j.get("key") for j in joker_slots.get("cards", [])}
                if owned_keys & SCALING_JOKERS:
                    passed_on.append(f"{label}(${cost}, would eat scaling joker)")
                    continue
            # Don't buy scaling jokers if Madness is owned — it'll eat them
            if key in SCALING_JOKERS:
                owned_keys = {j.get("key") for j in joker_slots.get("cards", [])}
                if "j_madness" in owned_keys:
                    passed_on.append(f"{label}(${cost}, Madness would eat it)")
                    continue
            # Slow flat scalers (chips/mult) are worthless late game.
            # xMult scalers still compound meaningfully — let them through
            # to be evaluated by the ante-aware composition multiplier.
            SLOW_SCALERS = (SCALING_JOKERS | {"j_madness", "j_ceremonial"}) - SCALING_XMULT
            if key in SLOW_SCALERS and ante >= 6:
                passed_on.append(f"{label}(${cost}, too late for slow scaler at ante {ante})")
                continue
            # Compute unified value for this candidate
            value = evaluate_joker_value(
                card, owned_jokers=joker_slots.get("cards", []),
                hand_levels=state.get("hands", {}), ante=ante,
                strategy=strat, joker_limit=jlimit,
            )

            # Override gates: force-buy known-good jokers (skip interest gating)
            force_buy = False
            if key in self.ALWAYS_BUY:
                value = max(value, 10.0)
                force_buy = True
            elif key in self.HIGH_PRIORITY and ante <= 5:
                value = max(value, 8.0)
                force_buy = True

            # Stencil restriction: filling slots reduces its ×mult
            # Stencil counts empty slots + all Stencils (including itself)
            if any(j.get("key") == "j_stencil" for j in joker_slots.get("cards", [])):
                stencil_count = sum(1 for j in joker_slots.get("cards", []) if j.get("key") == "j_stencil")
                stencil_mult_after = (jlimit - joker_count - 1) + stencil_count
                if stencil_mult_after <= 2 and value < 5.0:
                    passed_on.append(f"{label}(${cost}, Stencil restriction: only ×{stencil_mult_after} left)")
                    continue

            # First joker: buy anything — 0 jokers is a death sentence
            if joker_count == 0:
                value = max(value, 5.0)
                force_buy = True

            # Interest gating for mid-game (ante 3-4) — skip for force-buy jokers
            if not force_buy and 3 <= ante <= 4 and joker_count > 2:
                interest_after = self._interest_after(money, cost)
                loses_interest = interest_after < current_interest

                if loses_interest:
                    if money >= self.INTEREST_CAP:
                        if value < self.MIN_VALUE:
                            passed_on.append(f"{label}(${cost}, value={value:.1f} below threshold)")
                            continue
                    else:
                        if cost > 2:
                            passed_on.append(f"{label}(${cost}, saving for interest)")
                            continue

            # Slot pressure: last slot needs higher value
            if joker_count >= jlimit - 1:
                value *= 0.5

            if value > best_improvement:
                best_improvement = value
                best_idx = i
                best_cost = cost
                best_label = card.get("label", "?")

        if best_idx is not None:
            key = shop.get("cards", [])[best_idx].get("key", "")
            tier = "ALWAYS_BUY" if key in self.ALWAYS_BUY else (
                "HIGH_PRIORITY" if key in self.HIGH_PRIORITY else "scored")
            log.info("[SHOP] %s($%d): %s, value=%.1f — BUYING",
                     best_label, best_cost, tier, best_improvement)
            return BuyCard(
                best_idx,
                reason=f"buy joker: {best_label} for ${best_cost} "
                       f"(value={best_improvement:.1f}, ${money}->${money - best_cost})",
            )
        if passed_on:
            log.info("Passed on jokers: %s", ", ".join(passed_on))
        return None


class BuyConsumablesInShop:
    """Buy consumables from the shop using dynamic value scoring."""
    name = "buy_consumables_in_shop"

    INTEREST_CAP = 25

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        from balatro_bot.rules._helpers import score_consumable

        money = state.get("money", 0)
        shop = state.get("shop", {})
        consumables = state.get("consumables", {})

        # Need room in consumable slots
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

            # Only score consumable-type cards
            if card_set not in ("TAROT", "PLANET", "SPECTRAL") and key not in PLANET_KEYS:
                continue

            value = score_consumable(key, state, strat)
            if value <= 0:
                passed_on.append(f"{label}(value={value:.1f})")
                continue

            if cost > money:
                passed_on.append(f"{label}(${cost}, can't afford)")
                continue

            # Respect interest below cap
            current_interest = min(money // 5, 5)
            if money < self.INTEREST_CAP:
                interest_after = min((money - cost) // 5, 5)
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


class BuyPacksInShop:
    """Buy packs from the shop, prioritizing Planet > Buffoon > Tarot."""
    name = "buy_packs_in_shop"

    INTEREST_CAP = 25

    # Pack type priority by label keyword. Lower = buy first.
    # Detected by checking if the keyword appears in the card label.
    PACK_PRIORITY = {
        "Celestial": 1,   # Planet packs
        "Buffoon": 2,     # Joker packs
        # Standard packs intentionally excluded — they dilute the deck
        "Arcana": 3,      # Tarot packs
    }
    # Standard and Spectral packs intentionally omitted

    def _pack_priority(self, label: str) -> int | None:
        for keyword, priority in self.PACK_PRIORITY.items():
            if keyword in label:
                return priority
        return None

    def _interest_after(self, money: int, cost: int) -> int:
        return min((money - cost) // 5, 5)

    # All pack keywords for Red Card buying (buy any pack just to skip it)
    ALL_PACK_KEYWORDS = {"Celestial", "Buffoon", "Arcana", "Standard", "Spectral"}

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        money = state.get("money", 0)
        packs = state.get("packs", {})
        joker_slots = state.get("jokers", {})
        owned_jokers = joker_slots.get("cards", [])
        has_red_card = any(j.get("key") == "j_red_card" for j in owned_jokers)
        has_constellation = any(j.get("key") == "j_constellation" for j in owned_jokers)

        best_idx = None
        best_priority = 999
        best_cost = 0
        best_label = ""

        passed_on: list[str] = []

        for i, card in enumerate(packs.get("cards", [])):
            label = card.get("label", "")
            cost = card.get("cost", {}).get("buy", 999)
            if cost > money:
                passed_on.append(f"{label}(${cost}, can't afford)")
                continue

            # Red Card: buy ANY pack to skip it for +3 mult
            if has_red_card and any(kw in label for kw in self.ALL_PACK_KEYWORDS):
                if cost <= 4 or money >= self.INTEREST_CAP:
                    priority = 0
                else:
                    passed_on.append(f"{label}(${cost}, Red Card but too expensive)")
                    continue
            # Constellation: Celestial packs are top priority (every planet = +0.1 xMult)
            elif has_constellation and "Celestial" in label:
                priority = 0
            else:
                priority = self._pack_priority(label)
                if priority is None:
                    if "Spectral" in label and state.get("ante_num", 1) >= 3:
                        priority = 4
                    else:
                        passed_on.append(f"{label}(${cost}, not in buy list)")
                        continue

            # Skip Buffoon packs if joker slots are full
            if "Buffoon" in label and not has_red_card:
                if joker_slots.get("count", 0) >= joker_slots.get("limit", 5):
                    passed_on.append(f"{label}(${cost}, joker slots full)")
                    continue

            # Interest check — never drop below the next $5 threshold
            # Bypass for Red Card (buying to skip) and Constellation + Celestial
            skip_interest = has_red_card or (has_constellation and "Celestial" in label)
            if not skip_interest:
                current_interest = min(money // 5, 5)
                interest_after = self._interest_after(money, cost)
                loses_interest = interest_after < current_interest
                if loses_interest:
                    if money >= self.INTEREST_CAP:
                        if "Celestial" not in label:
                            passed_on.append(f"{label}(${cost}, would lose interest)")
                            continue
                    else:
                        passed_on.append(f"{label}(${cost}, saving for interest)")
                        continue

            if priority < best_priority:
                best_priority = priority
                best_idx = i
                best_cost = cost
                best_label = label

        if best_idx is not None:
            reason = f"buy pack: {best_label} for ${best_cost} (${money}->${money - best_cost})"
            if has_red_card:
                reason += " [Red Card: +3 mult on skip]"
            return BuyPack(best_idx, reason=reason)
        if passed_on:
            log.info("Passed on packs: %s", ", ".join(passed_on))
        return None


class BuyVouchersInShop:
    """Buy high-impact vouchers when we can afford them."""
    name = "buy_vouchers_in_shop"

    INTEREST_CAP = 25

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
        money = state.get("money", 0)
        vouchers = state.get("vouchers", {})

        # Only buy vouchers when we're at the interest cap with surplus
        # Vouchers cost $10 — don't dip below $25
        if money < self.INTEREST_CAP + 10:
            return None

        best_idx = None
        best_priority = 999
        best_cost = 0
        best_label = ""

        for i, card in enumerate(vouchers.get("cards", [])):
            key = card.get("key", "")
            priority = self.VOUCHER_PRIORITY.get(key)
            if priority is None:
                continue

            cost = card.get("cost", {}).get("buy", 999)
            if cost > money - self.INTEREST_CAP:
                # Don't spend below interest cap
                continue

            if priority < best_priority:
                best_priority = priority
                best_idx = i
                best_cost = cost
                best_label = card.get("label", "?")

        if best_idx is not None:
            return BuyVoucher(
                best_idx,
                reason=f"buy voucher: {best_label} for ${best_cost} (${money}->${money - best_cost})",
            )
        return None


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
        money = state.get("money", 0)
        round_num = state.get("round_num", 0)

        # Reset counter on new shop visit
        if round_num != self._last_round:
            self._rerolls_this_shop = 0
            self._last_round = round_num

        if self._rerolls_this_shop >= self.MAX_REROLLS:
            return None

        # Need enough money that rerolling doesn't hurt our interest
        if money < self.MIN_MONEY_TO_REROLL:
            return None

        # Don't reroll if there's a good joker we should buy
        shop = state.get("shop", {})
        joker_slots = state.get("jokers", {})
        has_open_slots = joker_slots.get("count", 0) < joker_slots.get("limit", 5)

        if has_open_slots:
            # Check if any joker in shop has a scoring effect
            for card in shop.get("cards", []):
                if card.get("set") == "JOKER":
                    key = card.get("key", "")
                    effect = JOKER_EFFECTS.get(key)
                    if effect is not None and effect is not _noop:
                        cost = card.get("cost", {}).get("buy", 999)
                        if cost <= money:
                            return None  # good joker available, buy instead

        strat = compute_strategy(
            state.get("jokers", {}).get("cards", []), state.get("hands", {})
        )
        strat_str = ", ".join(n for n, _ in strat.preferred_hands[:2]) if strat.preferred_hands else "no strategy yet"
        self._rerolls_this_shop += 1
        return Reroll(reason=f"reroll shop (${money}, looking for {strat_str} jokers)")


class LeaveShop:
    """When done shopping, move to next round."""
    name = "leave_shop"

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        return NextRound(reason="done shopping")
