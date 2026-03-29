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
from balatro_bot.hand_evaluator import best_hand
from balatro_bot.strategy import Strategy, compute_strategy, JOKER_HAND_AFFINITY
from balatro_bot.joker_effects import JOKER_EFFECTS, _noop, parse_effect_value

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

_UTILITY_VALUE: dict[str, float] = {
    # Tier 1: survival/game-changing
    "j_chicot":        4.0,   # destroys boss blind effect
    "j_mr_bones":      3.5,   # prevents death
    "j_perkeo":        3.0,   # duplicates consumable in shop
    # Tier 2: hand construction (understood by hand evaluator)
    "j_four_fingers":  2.5,   # 4-card straights/flushes
    "j_smeared":       2.5,   # merged suit groups
    "j_shortcut":      2.0,   # gap straights
    "j_splash":        2.0,   # all cards score
    "j_pareidolia":    2.0,   # all cards count as face
    # Tier 3: resource generation
    "j_cartomancer":   2.0,   # tarot on blind select
    "j_hallucination": 1.5,   # tarot from packs
    "j_space":         1.5,   # chance to level hand type
    "j_8_ball":        1.5,   # score 8 → tarot
    "j_sixth_sense":   1.0,   # play 6 → spectral
    "j_seance":        1.0,   # spectral from hand types
    "j_superposition": 1.0,   # Ace + Straight → tarot
    "j_riff_raff":     1.0,   # 2 common jokers on blind select
    "j_oops":          1.5,   # doubles all probabilities
    # Tier 4: hand size/discards
    "j_merry_andy":    1.5,   # +3 discards, -1 hand size
    "j_turtle_bean":   1.5,   # +5 hand size (decays)
    "j_drunkard":      1.0,   # +1 discard
    "j_burnt":         1.0,   # +1 discard
    "j_juggler":       1.0,   # +1 hand size
    "j_troubadour":    1.0,   # +2 hand size, -1 hand
    # Tier 5: economy/deck manipulation
    "j_vagabond":      0.5,   # tarot when money < $5
    "j_marble":        0.5,   # Stone card on blind select
    "j_dna":           0.5,   # copies first played card
    "j_certificate":   0.5,   # random card + gold seal
    "j_midas_mask":    0.5,   # face cards → Gold
    # Tier 6: retrigger jokers (scoring via retrigger_count, not direct effects)
    "j_hack":          1.5,   # retrigger 2,3,4,5 — rank affinity build
    "j_hanging_chad":  1.0,   # retrigger first scored card
    "j_dusk":          1.5,   # retrigger all on last hand
    "j_sock_and_buskin":1.0,  # retrigger face cards
    "j_seltzer":       1.5,   # retrigger all scored cards (limited uses)
    "j_mime":          1.0,   # retrigger held cards
    # Tier 7: triggered utility (action rules handle these)
    "j_luchador":      1.0,   # sell to disable boss blind
    "j_invisible":     1.0,   # sell to dupe after 2 rounds
    "j_diet_cola":     0.5,   # sell for free reroll
    "j_burglar":       0.5,   # +3 hands, lose discards
    "j_ring_master":   0.5,   # uncommon+ jokers more common
}


def _utility_synergy(key: str, owned_keys: set[str], strat: Strategy) -> float:
    """Bonus value for a utility joker based on current build synergy."""
    bonus = 0.0

    if key == "j_four_fingers":
        if strat.hand_affinity("Flush") > 0 or strat.hand_affinity("Straight") > 0:
            bonus += 2.0
    elif key == "j_smeared" and strat.hand_affinity("Flush") > 0:
        bonus += 3.0
    elif key == "j_shortcut" and strat.hand_affinity("Straight") > 0:
        bonus += 2.0
    elif key == "j_pareidolia":
        face_jokers = {"j_photograph", "j_scary_face", "j_smiley",
                        "j_triboulet", "j_sock_and_buskin"}
        bonus += len(owned_keys & face_jokers) * 2.0
    elif key == "j_8_ball" and strat.rank_affinity("8") > 0:
        bonus += 2.0
    elif key == "j_oops":
        prob_jokers = {"j_8_ball", "j_space", "j_sixth_sense",
                        "j_bloodstone", "j_lucky_cat"}
        bonus += len(owned_keys & prob_jokers) * 1.5
    elif key == "j_space" and strat.top_hand():
        bonus += 1.0
    elif key == "j_merry_andy":
        discard_jokers = {"j_castle", "j_yorick", "j_hit_the_road"}
        bonus += len(owned_keys & discard_jokers) * 2.0
    elif key == "j_marble" and "j_stone" in owned_keys:
        bonus += 2.0
    elif key == "j_splash":
        per_card = {"j_hiker", "j_seltzer", "j_hanging_chad"}
        bonus += len(owned_keys & per_card) * 1.5

    return bonus


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

        # No scoring effect — check if it's a valued utility joker
        if effect is None or effect is _noop:
            base = _UTILITY_VALUE.get(key, 0.0)
            if base <= 0:
                return 0.0
            owned_keys = {j.get("key") for j in (owned_jokers or [])}
            return base + _utility_synergy(key, owned_keys, strat)

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
        always_protected = {"j_madness", "j_ceremonial"} | SCALING_XMULT
        if shop_has_xmult_buy:
            protected = always_protected  # flat scalers become sellable
        else:
            protected = always_protected | SCALING_JOKERS

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
        money = state.get("money", 0)
        sell_value = weakest_joker.get("cost", {}).get("sell", 0)

        INTEREST_CAP = 25
        current_interest = min(money // 5, 5)

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

            shop_key = card.get("key", "")
            is_high_tier_xmult = shop_key in _HIGH_VALUE_XMULT

            # Dynamic threshold: much harder to justify selling on-strategy
            # jokers for off-strategy replacements
            weakest_key = weakest_joker.get("key", "")
            weakest_synergy = sum(
                strat.hand_affinity(ht)
                for ht in JOKER_HAND_AFFINITY.get(weakest_key, ([], 0))[0]
            )
            shop_synergy = sum(
                strat.hand_affinity(ht)
                for ht in JOKER_HAND_AFFINITY.get(shop_key, ([], 0))[0]
            )

            if is_high_tier_xmult:
                threshold = 0.5  # high-tier xMult: low bar to sell for it
            elif weakest_synergy > 0 and shop_synergy == 0:
                threshold = 3.0  # selling on-strategy for off-strategy: huge bar
            elif weakest_synergy > 0 and shop_synergy > 0:
                threshold = 1.5  # on-strategy swap: still needs clear improvement
            else:
                threshold = 1.0  # base

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

    def __init__(self) -> None:
        self._last_order: list[int] | None = None

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
            self._last_order = None
            return None

        # Classify each non-Ceremonial joker as valuable or fodder.
        # Any utility joker with base value >= 1.0 is too useful to sacrifice.
        valuable = []
        fodder = []
        blueprint_idx = None
        brainstorm_idx = None
        for i, j in enumerate(owned):
            if j.get("key") == "j_ceremonial":
                continue
            key = j.get("key", "")
            effect = JOKER_EFFECTS.get(key)
            if key == "j_blueprint":
                blueprint_idx = i
                valuable.append(i)
            elif key == "j_brainstorm":
                brainstorm_idx = i
                valuable.append(i)
            elif key in ({"j_madness"} | SCALING_JOKERS):
                valuable.append(i)
            elif _UTILITY_VALUE.get(key, 0.0) >= 1.0:
                valuable.append(i)
            elif effect is None or effect is _noop:
                fodder.append(i)
            else:
                valuable.append(i)

        # Pick fodder deterministically by joker key (not position) to avoid
        # oscillation when multiple fodder jokers swap positions each rearrange.
        if fodder:
            fodder.sort(key=lambda i: owned[i].get("key", ""))
            sacrifice_idx = fodder[0]
            rest_fodder = fodder[1:]
            desired_order = valuable + rest_fodder + [ceremonial_idx] + [sacrifice_idx]
        else:
            desired_order = valuable + [ceremonial_idx]

        # Blueprint: ensure it's not immediately left of Ceremonial or fodder.
        # Move it to the start of valuable (so it copies the joker to its right).
        if blueprint_idx is not None and len(valuable) >= 2:
            desired_order = [i for i in desired_order if i != blueprint_idx]
            # Place Blueprint at position 0 (copies position 1, a valuable joker)
            desired_order.insert(0, blueprint_idx)

        current_order = list(range(len(owned)))
        if desired_order == current_order:
            self._last_order = None
            return None

        # Cycle guard: if we just rearranged to this exact order, don't do it again
        if self._last_order == desired_order:
            return None
        self._last_order = desired_order

        sacrifice_label = owned[fodder[0]].get("label", "?") if fodder else "none"
        return RearrangeJokers(
            order=desired_order,
            reason=f"reorder for Ceremonial Dagger: fodder={sacrifice_label}",
        )


class BuyJokersInShop:
    """Buy jokers that improve scoring, respecting interest thresholds."""
    name = "buy_jokers_in_shop"

    INTEREST_CAP = 25
    # Minimum score improvement (%) to justify a purchase that loses interest
    MIN_IMPROVEMENT = 0.10

    ALWAYS_BUY = _ALWAYS_BUY
    HIGH_PRIORITY = _HIGH_PRIORITY

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

    def _composition_multiplier(self, owned_jokers: list, candidate_key: str, ante: int = 1) -> float:
        """Weight candidate by how much it fills a scoring gap in the current build.

        Returns >1.0 if candidate fills an underrepresented category,
        <1.0 if it stacks an already-full category, 1.0 if neutral/utility.

        At higher antes, xMult need is amplified and flat mult/chips need is
        dampened — reflecting that blinds scale exponentially but flat bonuses
        don't.
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

        # Ante-based urgency: xMult becomes critical as blinds scale exponentially
        # Ante 1-3: no adjustment. Ante 4+: xmult amplified, flat dampened.
        if ante >= 4:
            urgency = min(1.0 + (ante - 3) * 0.5, 3.0)    # 1.5 @ ante 4 → 3.0 @ ante 7+
            dampen = max(1.0 - (ante - 3) * 0.125, 0.5)    # 0.875 @ ante 4 → 0.5 @ ante 7+
            needs["xmult"] *= urgency
            needs["mult"] *= dampen
            needs["chips"] *= dampen

        avg_need = sum(needs.values()) / len(needs)

        if avg_need == 0:
            return 1.0

        raw = needs[candidate_cat] / avg_need
        cap = 3.5 if candidate_cat == "xmult" else 2.0
        return max(0.5, min(cap, raw))

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
                # Check if it's a valued utility joker
                base = _UTILITY_VALUE.get(key, 0.0)
                if base > 0:
                    strat = compute_strategy(current_jokers, state.get("hands", {}))
                    owned_keys = {j.get("key") for j in current_jokers}
                    return (base + _utility_synergy(key, owned_keys, strat)) / 10.0
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
            if key in self.ALWAYS_BUY:
                improvement = 1.0  # force-buy
            elif key in self.HIGH_PRIORITY and ante <= 5:
                improvement = 0.8  # scaling xMult — strong buy early
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
            improvement *= self._composition_multiplier(joker_slots.get("cards", []), key, ante)

            if improvement > best_improvement:
                best_improvement = improvement
                best_idx = i
                best_cost = cost
                best_label = card.get("label", "?")

        if best_idx is not None:
            key = shop.get("cards", [])[best_idx].get("key", "")
            comp = self._composition_multiplier(joker_slots.get("cards", []), key, ante)
            tier = "ALWAYS_BUY" if key in self.ALWAYS_BUY else (
                "HIGH_PRIORITY" if key in self.HIGH_PRIORITY else "scored")
            log.info("[SHOP] %s($%d): %s, improvement=%.0f%%, comp=%.1fx — BUYING",
                     best_label, best_cost, tier, best_improvement * 100, comp)
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
                has_constellation = any(j.get("key") == "j_constellation" for j in jokers)
                if hand_type == "ALL":
                    priority = 1  # Black Hole — always buy
                elif hand_type and strat.hand_affinity(hand_type) > 0:
                    priority = 1  # levels a hand we care about
                elif has_constellation:
                    priority = 2  # Constellation: every planet = +0.1 xmult
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
