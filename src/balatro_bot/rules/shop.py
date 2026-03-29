from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from balatro_bot.actions import (
    BuyCard, BuyPack, BuyVoucher, SellJoker, SellConsumable,
    Reroll, NextRound, RearrangeJokers, Action,
)
from balatro_bot.context import RoundContext
from balatro_bot.constants import (
    SCALING_JOKERS, PLANET_KEYS, SAFE_CONSUMABLE_TAROTS,
    TARGETING_TAROTS, SAFE_SPECTRAL_CONSUMABLES, SPECTRAL_TARGETING,
)
from balatro_bot.hand_evaluator import best_hand
from balatro_bot.strategy import Strategy, compute_strategy, JOKER_HAND_AFFINITY
from balatro_bot.joker_effects import JOKER_EFFECTS, _noop, parse_effect_value

if TYPE_CHECKING:
    from typing import Any

log = logging.getLogger("balatro_bot")


class SellWeakJoker:
    """Sell the weakest joker if slots are full and the shop has a better one."""
    name = "sell_weak_joker"

    # Approximate power tier for unconditional jokers (no hand type affinity).
    # Higher = harder to justify selling.
    UNCONDITIONAL_POWER: dict[str, float] = {
        # xmult — never sell these
        "j_cavendish": 5.0,    # X3
        "j_madness": 6.0,     # scaling X0.5 per blind — run-defining
        "j_stencil": 4.0,     # X1 per empty slot
        # Strong +mult
        "j_gros_michel": 3.0, # +15 mult
        "j_popcorn": 2.5,     # +20 mult (decays)
        "j_misprint": 1.5,    # +11 avg mult
        # Scaling — get better over time, don't sell late
        "j_supernova": 2.5,   # +mult per time hand type played this run
        "j_green_joker": 2.0, # +1 mult per hand, scaling
        "j_ride_the_bus": 2.0,# +1 mult per hand without face card
        "j_flash": 2.0,       # +2 mult per reroll
        "j_constellation": 3.0,# X0.1 per planet used — scaling xmult
        "j_campfire": 2.5,    # X0.25 per sell
        # Moderate
        "j_ice_cream": 1.5,   # +100 chips (decays)
        "j_blue_joker": 1.2,  # +2 chips/deck card
        "j_stuntman": 2.0,    # +250 chips
        # Weak
        "j_joker": 0.8,       # +4 mult
    }

    def _joker_strategy_value(
        self, joker: dict, strat: Strategy, owned_jokers: list[dict] | None = None,
    ) -> float:
        """Score how valuable a joker is to our current strategy.

        Uses parsed effect text to assess actual accumulated value for
        scaling jokers, instead of relying solely on the static tier list.

        When owned_jokers is provided, adds a coherence bonus for jokers
        that share strategic hand types with other owned jokers — making
        it much harder to sell a joker that's part of a cohesive build.
        """
        key = joker.get("key", "")
        effect = JOKER_EFFECTS.get(key)

        # No scoring effect at all — lowest value
        if effect is None or effect is _noop:
            return 0.0

        # Parse the actual current value from the joker's effect text
        effect_text = joker.get("value", {}).get("effect", "")
        parsed = parse_effect_value(effect_text) if effect_text else {}

        # Compute a dynamic power score from parsed values
        # xmult jokers are most valuable, then +mult, then +chips
        dynamic_power = 0.0
        if parsed.get("xmult") and parsed["xmult"] > 1.0:
            dynamic_power = parsed["xmult"] * 2.0  # X2.5 → 5.0, X4 → 8.0
        if parsed.get("mult") and parsed["mult"] > 0:
            dynamic_power = max(dynamic_power, parsed["mult"] / 5.0)  # +30 mult → 6.0, +5 → 1.0
        if parsed.get("chips") and parsed["chips"] > 0:
            dynamic_power = max(dynamic_power, parsed["chips"] / 50.0)  # +100 chips → 2.0

        # Unconditional jokers — use the higher of static tier or dynamic parsed value
        if key not in JOKER_HAND_AFFINITY:
            static_power = self.UNCONDITIONAL_POWER.get(key, 1.0)
            return max(static_power, dynamic_power)

        # Conditional joker — value depends on strategy alignment + dynamic power
        hand_types, weight = JOKER_HAND_AFFINITY[key]
        synergy = sum(strat.hand_affinity(ht) for ht in hand_types)

        # Build coherence bonus: count how many other owned jokers share
        # at least one hand type with this joker. A joker embedded in a
        # cohesive build (e.g. j_duo + j_jolly + j_sly all on Pair) is
        # worth far more than its individual score suggests.
        coherence_bonus = 0.0
        if owned_jokers and key in JOKER_HAND_AFFINITY:
            my_hands = set(JOKER_HAND_AFFINITY[key][0])
            allies = 0
            for other in owned_jokers:
                okey = other.get("key", "")
                if okey == key or okey not in JOKER_HAND_AFFINITY:
                    continue
                other_hands = set(JOKER_HAND_AFFINITY[okey][0])
                if my_hands & other_hands:
                    allies += 1
            coherence_bonus = allies * 1.5  # each ally adds 1.5 to sell resistance

        if synergy > 0:
            return max(2.0 + synergy + coherence_bonus, dynamic_power)
        return max(0.5 + coherence_bonus, dynamic_power)

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

        # Score each owned joker — never sell scaling jokers, Madness, or
        # Ceremonial Dagger itself
        protected = {"j_madness", "j_ceremonial"} | SCALING_JOKERS
        owned_values = [
            (i, self._joker_strategy_value(j, strat, owned_jokers=owned), j)
            for i, j in enumerate(owned)
            if j.get("key") not in protected
        ]
        if not owned_values:
            return None

        # Find the weakest joker
        weakest_idx, weakest_value, weakest_joker = min(owned_values, key=lambda x: x[1])

        # Check if the shop has a better joker available
        shop = state.get("shop", {})
        money = state.get("money", 0)
        sell_value = weakest_joker.get("cost", {}).get("sell", 0)

        INTEREST_CAP = 25
        current_interest = min(money // 5, 5)
        ante = state.get("ante_num", 1)

        for card in shop.get("cards", []):
            if card.get("set") != "JOKER":
                continue
            cost = card.get("cost", {}).get("buy", 999)
            money_after_sell = money + sell_value
            if cost > money_after_sell:
                continue

            # Check that BuyJokersInShop won't block this buy due to interest
            # (mirrors its logic: skip if losing interest below cap, unless ante >= 5)
            if ante < 5 and money_after_sell < INTEREST_CAP:
                interest_after_buy = min((money_after_sell - cost) // 5, 5)
                if interest_after_buy < current_interest:
                    continue  # buy would be blocked by interest threshold

            # Score the shop joker WITHOUT coherence bonus (it has no allies yet)
            shop_value = self._joker_strategy_value(card, strat)

            # Dynamic threshold: much harder to justify selling on-strategy
            # jokers for off-strategy replacements
            weakest_key = weakest_joker.get("key", "")
            weakest_synergy = sum(
                strat.hand_affinity(ht)
                for ht in JOKER_HAND_AFFINITY.get(weakest_key, ([], 0))[0]
            )
            shop_key = card.get("key", "")
            shop_synergy = sum(
                strat.hand_affinity(ht)
                for ht in JOKER_HAND_AFFINITY.get(shop_key, ([], 0))[0]
            )

            threshold = 1.0  # base: shop joker must be 1.0 better
            if weakest_synergy > 0 and shop_synergy == 0:
                threshold = 3.0  # selling on-strategy for off-strategy: huge bar
            elif weakest_synergy > 0 and shop_synergy > 0:
                threshold = 1.5  # on-strategy swap: still needs clear improvement

            if shop_value > weakest_value + threshold:
                shop_label = card.get("label", "?")
                return SellJoker(
                    weakest_idx,
                    reason=f"sell {weakest_joker.get('label', '?')} (value={weakest_value:.1f}) "
                           f"for {shop_label} (value={shop_value:.1f}, threshold={threshold:.1f}) "
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

            # Hex: sell if it would destroy our lineup (same gates as UseImmediateConsumables)
            if key == "c_hex":
                joker_count = state.get("jokers", {}).get("count", 0)
                joker_limit = state.get("jokers", {}).get("limit", 5)
                owned_keys = {j.get("key") for j in owned_jokers}
                ante = state.get("ante_num", 1)
                if joker_count >= joker_limit or owned_keys & SCALING_JOKERS or ante >= 5:
                    return SellConsumable(
                        i, reason=f"Campfire: sell Hex (+X0.25 Mult, would destroy lineup)",
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


class ReorderJokersForCeremonial:
    """When Ceremonial Dagger is owned, arrange jokers so only fodder gets eaten.

    Ceremonial Dagger destroys the joker immediately to its right at the start
    of each blind, gaining permanent mult. Layout:
      [valuable jokers] [Ceremonial Dagger] [fodder OR nothing]

    Fodder = jokers with no scoring effect (_noop) or very low strategy value.
    If no fodder exists, Ceremonial goes rightmost and eats nothing.
    """
    name = "reorder_jokers_for_ceremonial"

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        ctx = RoundContext.from_state(state)
        # Amber Acorn reshuffles jokers every hand — reordering is immediately undone
        if ctx.blind_name == "Amber Acorn":
            return None

        joker_info = state.get("jokers", {})
        owned = joker_info.get("cards", [])
        if len(owned) < 2:
            return None

        ceremonial_idx = next(
            (i for i, j in enumerate(owned) if j.get("key") == "j_ceremonial"), None
        )
        if ceremonial_idx is None:
            return None

        # Classify each non-Ceremonial joker as valuable or fodder
        # Fodder: no scoring effect, or economy-only jokers
        valuable = []
        fodder = []
        for i, j in enumerate(owned):
            if j.get("key") == "j_ceremonial":
                continue
            key = j.get("key", "")
            effect = JOKER_EFFECTS.get(key)
            # Protected jokers are always valuable
            if key in ({"j_madness"} | SCALING_JOKERS):
                valuable.append(i)
            elif effect is None or effect is _noop:
                fodder.append(i)  # no scoring effect — safe to sacrifice
            else:
                valuable.append(i)  # has a scoring effect — keep it

        # Desired layout: [valuable] [ceremonial] [one fodder if available]
        if fodder:
            desired_order = valuable + [ceremonial_idx] + [fodder[0]]
            # Remaining fodder (if multiple) go before ceremonial
            desired_order = valuable + fodder[1:] + [ceremonial_idx] + [fodder[0]]
        else:
            # No fodder — park Ceremonial rightmost, eats nothing
            desired_order = valuable + [ceremonial_idx]

        current_order = list(range(len(owned)))
        if desired_order == current_order:
            return None

        fodder_label = owned[fodder[0]].get("label", "?") if fodder else "none"
        return RearrangeJokers(
            order=desired_order,
            reason=f"reorder for Ceremonial Dagger: fodder={fodder_label}",
        )


class BuyJokersInShop:
    """Buy jokers that improve scoring, respecting interest thresholds."""
    name = "buy_jokers_in_shop"

    INTEREST_CAP = 25
    # Minimum score improvement (%) to justify a purchase that loses interest
    MIN_IMPROVEMENT = 0.10

    # Jokers worth buying regardless of money — these are run-defining.
    # xmult jokers and strong unconditional mult/chips.
    ALWAYS_BUY = {
        # Unconditional xmult
        "j_cavendish",      # X3
        "j_stencil",        # X1 per empty slot
        # Hand-type xmult
        "j_duo",            # X2 on Pair
        "j_trio",           # X3 on Three of a Kind
        "j_family",         # X4 on Four of a Kind
        "j_order",          # X3 on Straight
        "j_tribe",          # X2 on Flush
        # Strong unconditional
        "j_gros_michel",    # +15 mult
        "j_popcorn",        # +20 mult (decays but huge early)
        # Strong conditional xmult
        "j_acrobat",        # X3 on final hand
        "j_blackboard",     # X3 if held cards spades/clubs
        "j_flower_pot",     # X3 if all 4 suits
        # Scaling xmult
        "j_madness",        # +X0.5 per blind, eats a joker (needs fodder)
    }

    # Primary scoring category for each joker — used for build composition weighting.
    # Jokers not listed here (utility, economy, copy) return a neutral 1.0 multiplier.
    JOKER_SCORE_CATEGORY: dict[str, set[str]] = {
        "xmult": {
            # Unconditional
            "j_cavendish", "j_stencil",
            # Hand-type
            "j_duo", "j_trio", "j_family", "j_order", "j_tribe",
            # Card property
            "j_photograph", "j_baron", "j_bloodstone", "j_triboulet",
            # Game-state conditional
            "j_blackboard", "j_acrobat", "j_flower_pot", "j_seeing_double",
            "j_steel_joker", "j_loyalty_card", "j_drivers_license",
            # Scaling
            "j_madness", "j_vampire", "j_hologram", "j_obelisk",
            "j_lucky_cat", "j_glass", "j_campfire", "j_throwback",
            "j_card_sharp", "j_ancient", "j_baseball", "j_canio",
            "j_yorick", "j_hit_the_road", "j_constellation", "j_idol",
        },
        "mult": {
            # Unconditional
            "j_joker", "j_misprint", "j_gros_michel", "j_popcorn",
            # Hand-type
            "j_jolly", "j_zany", "j_mad", "j_crazy", "j_droll",
            # Suit conditional
            "j_greedy_joker", "j_lusty_joker", "j_wrathful_joker", "j_gluttenous_joker",
            "j_onyx_agate",
            # Card property
            "j_smiley", "j_fibonacci", "j_even_steven",
            "j_shoot_the_moon", "j_raised_fist",
            # Game-state conditional
            "j_half", "j_abstract", "j_mystic_summit", "j_bootstraps",
            "j_swashbuckler", "j_erosion",
            # Scaling
            "j_ceremonial", "j_supernova", "j_ride_the_bus", "j_green_joker",
            "j_red_card", "j_flash", "j_fortune_teller", "j_trousers", "j_ramen",
        },
        "chips": {
            # Unconditional
            "j_blue_joker", "j_stuntman", "j_ice_cream",
            # Hand-type
            "j_sly", "j_wily", "j_clever", "j_devious", "j_crafty",
            # Suit conditional
            "j_arrowhead",
            # Card property
            "j_scary_face", "j_odd_todd",
            # Game-state conditional
            "j_banner", "j_bull",
            # Scaling
            "j_runner", "j_square", "j_castle", "j_wee", "j_hiker", "j_stone",
        },
    }

    def _composition_multiplier(self, owned_jokers: list, candidate_key: str) -> float:
        """Weight candidate by how much it fills a scoring gap in the current build.

        Returns >1.0 if candidate fills an underrepresented category,
        <1.0 if it stacks an already-full category, 1.0 if neutral/utility.

        Formula: need(category) = 1/(1+count), normalized against the average
        need across all three categories. A perfectly balanced build (1/1/1) yields
        1.0 for all candidates. A build with 0 xmult and 2 mult/chips yields ~1.5
        for xmult candidates and ~0.75 for mult/chips candidates.
        """
        candidate_cat = None
        for cat, keys in self.JOKER_SCORE_CATEGORY.items():
            if candidate_key in keys:
                candidate_cat = cat
                break

        if candidate_cat is None:
            return 1.0  # utility/economy/copy jokers — neutral

        counts: dict[str, int] = {"xmult": 0, "mult": 0, "chips": 0}
        for j in owned_jokers:
            k = j.get("key", "")
            for cat, keys in self.JOKER_SCORE_CATEGORY.items():
                if k in keys:
                    counts[cat] += 1
                    break

        needs = {cat: 1.0 / (1.0 + counts[cat]) for cat in counts}
        avg_need = sum(needs.values()) / len(needs)

        if avg_need == 0:
            return 1.0

        raw = needs[candidate_cat] / avg_need
        return max(0.5, min(2.0, raw))

    def _interest_after(self, money: int, cost: int) -> int:
        return min((money - cost) // 5, 5)

    def _has_scoring_effect(self, joker_key: str) -> bool:
        """Check if a joker has a real scoring effect (not a no-op)."""
        effect = JOKER_EFFECTS.get(joker_key)
        return effect is not None and effect is not _noop

    def _score_improvement(self, state: dict, candidate_joker: dict) -> float:
        """Estimate how much a new joker improves our best hand score.

        Returns fractional improvement (0.5 = 50% better).
        If no hand is available (e.g. in shop), returns a positive value
        for jokers with known scoring effects so they still get bought.
        """
        hand_cards = state.get("hand", {}).get("cards", [])
        hand_levels = state.get("hands", {})
        current_jokers = state.get("jokers", {}).get("cards", [])

        if not hand_cards:
            # No hand to evaluate — use strategy to score the joker
            key = candidate_joker.get("key", "")
            if not self._has_scoring_effect(key):
                return 0.0

            strat = compute_strategy(current_jokers, state.get("hands", {}))

            # Joker that boosts our preferred hand type is worth more
            if key in JOKER_HAND_AFFINITY:
                hand_types, weight = JOKER_HAND_AFFINITY[key]
                synergy = sum(strat.hand_affinity(ht) for ht in hand_types)
                if synergy > 0:
                    return 0.40  # strong synergy
                return 0.15  # has effect but doesn't synergize

            return 0.20  # unconditional joker, always decent

        joker_limit = state.get("jokers", {}).get("limit", 5)
        current_best = best_hand(hand_cards, hand_levels, jokers=current_jokers, joker_limit=joker_limit)
        with_new = best_hand(hand_cards, hand_levels, jokers=current_jokers + [candidate_joker], joker_limit=joker_limit)

        if not current_best or not with_new:
            return 0.0

        if current_best.total == 0:
            return 1.0 if with_new.total > 0 else 0.0

        return (with_new.total - current_best.total) / current_best.total

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        money = state.get("money", 0)
        shop = state.get("shop", {})
        joker_slots = state.get("jokers", {})
        ante = state.get("ante_num", 1)

        if joker_slots.get("count", 0) >= joker_slots.get("limit", 5):
            return None

        current_interest = min(money // 5, 5)

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
            # Slow scaling jokers (start at X1.0, grow over many rounds) are
            # worthless late game — they won't compound fast enough to matter.
            # Ceremonial Dagger also needs many rounds of fodder to pay off.
            SLOW_SCALERS = SCALING_JOKERS | {"j_madness", "j_ceremonial"}
            if key in SLOW_SCALERS and ante >= 6:
                passed_on.append(f"{label}(${cost}, too late for slow scaler at ante {ante})")
                continue
            if key in self.ALWAYS_BUY:
                improvement = 1.0  # force-buy
            # Stencil restriction: filling slots reduces its ×mult
            elif any(j.get("key") == "j_stencil" for j in joker_slots.get("cards", [])):
                joker_limit = joker_slots.get("limit", 5)
                # After buying: empty slots = (limit - count - 1), +1 for Stencil counting itself
                stencil_mult_after = (joker_limit - joker_count - 1) + 1
                if stencil_mult_after <= 2:
                    # Buying would leave Stencil at ×1 or ×2 — need a very strong joker
                    improvement = self._score_improvement(state, card)
                    if improvement < 0.40:
                        passed_on.append(f"{label}(${cost}, Stencil restriction: only ×{stencil_mult_after} left)")
                        continue
                else:
                    improvement = self._score_improvement(state, card)
            # First joker: buy anything — 0 jokers is a death sentence
            elif joker_count == 0:
                improvement = 0.50
            # Joker-starved: ≤2 jokers means building a scoring engine beats saving interest
            elif joker_count <= 2:
                improvement = self._score_improvement(state, card)
            # Early game (ante ≤ 2): always prioritize jokers over saving interest
            elif state.get("ante_num", 1) <= 2:
                improvement = self._score_improvement(state, card)
            # Late game (ante ≥ 5): interest won't matter, just score the joker
            elif state.get("ante_num", 1) >= 5:
                improvement = self._score_improvement(state, card)
            else:
                interest_after = self._interest_after(money, cost)
                loses_interest = interest_after < current_interest

                if loses_interest:
                    if money >= self.INTEREST_CAP:
                        improvement = self._score_improvement(state, card)
                        if improvement < self.MIN_IMPROVEMENT:
                            passed_on.append(f"{label}(${cost}, +{improvement:.0%} below threshold)")
                            continue
                    else:
                        if cost > 2:
                            passed_on.append(f"{label}(${cost}, saving for interest)")
                            continue
                        improvement = self._score_improvement(state, card)
                else:
                    improvement = self._score_improvement(state, card)

            # Weight by build composition: fill gaps, don't stack full categories
            improvement *= self._composition_multiplier(joker_slots.get("cards", []), key)

            if improvement > best_improvement:
                best_improvement = improvement
                best_idx = i
                best_cost = cost
                best_label = card.get("label", "?")

        if best_idx is not None:
            return BuyCard(
                best_idx,
                reason=f"buy joker: {best_label} for ${best_cost} "
                       f"(+{best_improvement:.0%} score, ${money}->${money - best_cost})",
            )
        if passed_on:
            log.info("Passed on jokers: %s", ", ".join(passed_on))
        return None


class BuyConsumablesInShop:
    """Buy Planet cards and useful Tarots from the shop."""
    name = "buy_consumables_in_shop"

    INTEREST_CAP = 25

    # Planet card keys -> hand type they level (for strategy-aware buying)
    PLANET_KEYS = {
        "c_mercury": "Pair", "c_venus": "Three of a Kind", "c_earth": "Full House",
        "c_mars": "Four of a Kind", "c_jupiter": "Flush", "c_saturn": "Straight",
        "c_uranus": "Two Pair", "c_neptune": "Straight Flush", "c_pluto": "High Card",
        "c_planet_x": "Five of a Kind", "c_ceres": "Flush House", "c_eris": "Flush Five",
        "c_black_hole": "ALL",
    }

    # No-target Tarots worth buying from shop
    GOOD_TAROTS = {
        "c_judgement",         # creates random Joker
        "c_high_priestess",   # creates 2 random Planets
        "c_hermit",           # doubles money (max $20)
        "c_emperor",          # creates 2 random Tarots
    }

    # Targeting Tarots worth buying — Glass and suit conversions are high value
    GOOD_TARGETING_TAROTS = {
        "c_justice": 3,    # Glass — ×2 mult on face cards, very strong
        "c_star": 4,       # Suit conversions — strategy-dependent
        "c_moon": 4,
        "c_sun": 4,
        "c_world": 4,
        "c_lovers": 4,     # Enhancements — always useful
        "c_chariot": 4,
        "c_hierophant": 4,
        "c_empress": 4,
        "c_magician": 4,
        "c_devil": 4,
        "c_strength": 5,   # Rank up — situational
    }

    def evaluate(self, state: dict[str, Any]) -> Action | None:
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
        best_priority = 999
        best_cost = 0
        best_label = ""
        passed_on: list[str] = []

        for i, card in enumerate(shop.get("cards", [])):
            key = card.get("key", "")
            label = card.get("label", "?")
            card_set = card.get("set", "")
            cost = card.get("cost", {}).get("buy", 999)

            # Planet cards — on-strategy priority 1, off-strategy skip
            if key in self.PLANET_KEYS or card_set == "PLANET":
                hand_type = self.PLANET_KEYS.get(key)
                if hand_type == "ALL":
                    priority = 1  # Black Hole — always buy
                elif hand_type and strat.hand_affinity(hand_type) > 0:
                    priority = 1  # levels a hand we care about
                else:
                    passed_on.append(f"{label}({hand_type}, off-strategy)")
                    continue
            # Good no-target Tarots — priority 2
            elif key in self.GOOD_TAROTS:
                priority = 2
            # Good targeting Tarots — priority 3-5
            elif key in self.GOOD_TARGETING_TAROTS:
                priority = self.GOOD_TARGETING_TAROTS[key]
            else:
                if card_set in ("TAROT", "PLANET", "SPECTRAL"):
                    passed_on.append(f"{label}(not in buy list)")
                continue

            if cost > money:
                passed_on.append(f"{label}(${cost}, can't afford)")
                continue

            # Respect interest below cap (planets are cheap, usually $3-4)
            current_interest = min(money // 5, 5)
            if money < self.INTEREST_CAP:
                interest_after = min((money - cost) // 5, 5)
                if interest_after < current_interest and cost > 3:
                    passed_on.append(f"{label}(${cost}, saving for interest)")
                    continue

            if priority < best_priority or (priority == best_priority and cost < best_cost):
                best_priority = priority
                best_idx = i
                best_cost = cost
                best_label = card.get("label", "?")

        if best_idx is not None:
            return BuyCard(
                best_idx,
                reason=f"buy consumable: {best_label} for ${best_cost} (${money}->${money - best_cost})",
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
            if not has_red_card:
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
