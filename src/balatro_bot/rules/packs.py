from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from balatro_bot.actions import PackAction, Action
from balatro_bot.constants import (
    NO_TARGET_TAROTS, TARGETING_TAROTS, PLANET_HAND_MAP, PLANET_KEYS,
    SCALING_JOKERS, SAFE_SPECTRAL_CONSUMABLES, SPECTRAL_TARGETING,
)
from balatro_bot.strategy import compute_strategy, JOKER_HAND_AFFINITY

from balatro_bot.rules._helpers import _find_tarot_targets, evaluate_hex

if TYPE_CHECKING:
    from typing import Any

log = logging.getLogger("balatro_bot")


class SkipPackForRedCard:
    """Skip packs to trigger Red Card's +3 mult scaling.

    Skips Standard, Spectral, and Arcana packs. Does NOT skip Celestial
    (planet cards too valuable) or Buffoon (free jokers too valuable).
    """
    name = "skip_pack_for_red_card"

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        jokers = state.get("jokers", {}).get("cards", [])
        if not any(j.get("key") == "j_red_card" for j in jokers):
            return None

        pack = state.get("pack", {})
        cards = pack.get("cards", [])
        if not cards:
            return None

        # Don't skip if pack has Planets or Jokers — those are too valuable
        for c in cards:
            if c.get("set") == "PLANET" or c.get("label", "") in PLANET_HAND_MAP:
                return None
            if c.get("set") == "JOKER":
                return None
            if c.get("key", "") == "c_black_hole":
                return None

        return PackAction(card_index=None, reason="skip pack for Red Card (+3 mult)")


class PickFromTarotPack:
    """Pick the best Tarot card from an Arcana pack using dynamic scoring."""
    name = "pick_from_tarot_pack"

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        from balatro_bot.rules._helpers import score_consumable

        pack = state.get("pack", {})
        cards = pack.get("cards", [])
        if not cards:
            return None

        # Only handle Tarot packs
        known_keys = set(NO_TARGET_TAROTS) | set(TARGETING_TAROTS)
        if not any(c.get("key", "") in known_keys for c in cards):
            return None

        hand_cards = state.get("hand", {}).get("cards", [])
        jokers = state.get("jokers", {}).get("cards", [])
        hand_levels = state.get("hands", {})
        strat = compute_strategy(jokers, hand_levels)

        best_idx = None
        best_score = -1.0
        best_targets: list[int] | None = None

        for i, card in enumerate(cards):
            key = card.get("key", "")
            score = score_consumable(key, state, strat)
            if score <= best_score:
                continue

            # For targeting tarots, also verify we have valid targets
            targets = None
            if key in TARGETING_TAROTS and hand_cards:
                max_count, effect_type, extra = TARGETING_TAROTS[key]
                found_targets, _ = _find_tarot_targets(
                    effect_type, extra, max_count, hand_cards, jokers, strat,
                )
                if not found_targets:
                    continue  # can't use this tarot — no valid targets
                targets = found_targets

            best_score = score
            best_idx = i
            best_targets = targets

        if best_idx is not None:
            label = cards[best_idx].get("label", "?")
            key = cards[best_idx].get("key", "")
            if best_targets:
                return PackAction(
                    card_index=best_idx, targets=best_targets,
                    reason=f"tarot: {label} (value={best_score:.1f}) -> targets {best_targets}",
                )
            return PackAction(
                card_index=best_idx,
                reason=f"tarot: {label} (value={best_score:.1f})",
            )

        return PackAction(card_index=None, reason="skip tarot pack (nothing usable)")


class PickFromPlanetPack:
    """Pick the planet card that best synergizes with our strategy."""
    name = "pick_from_planet_pack"

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        pack = state.get("pack", {})
        cards = pack.get("cards", [])
        if not cards:
            return None

        # Only handle Planet packs — check if any card is a known planet or Black Hole
        is_planet_pack = any(
            c.get("label", "") in PLANET_HAND_MAP
            or c.get("key", "") in ("c_black_hole",)
            or c.get("label", "") == "Black Hole"
            for c in cards
        )
        if not is_planet_pack:
            return None

        # Black Hole levels ALL hand types — always pick it first
        for i, card in enumerate(cards):
            if card.get("key", "") == "c_black_hole" or card.get("label", "") == "Black Hole":
                return PackAction(card_index=i, reason=f"planet: Black Hole (levels ALL hand types!)")

        jokers = state.get("jokers", {}).get("cards", [])
        joker_keys = {j.get("key") for j in jokers}
        hand_levels = state.get("hands", {})
        strat = compute_strategy(jokers, hand_levels)
        has_constellation = "j_constellation" in joker_keys

        # Balance of playability (how often you get this hand) and scaling
        # potential (how well the base chips×mult grows with levels).
        # Pair is common but has a low ceiling. Flush/Straight are less
        # common but scale much better — leveling them pays off more.
        HAND_VALUE: dict[str, float] = {
            "Two Pair": 8, "Pair": 7,          # most common hands — best default
            "Full House": 6, "Flush": 6, "Straight": 6,
            "Three of a Kind": 5,
            "High Card": 4, "Four of a Kind": 4,
            "Straight Flush": 2, "Five of a Kind": 2,
            "Flush House": 2, "Flush Five": 2,
        }

        best_idx = 0
        best_score = -1.0

        for i, card in enumerate(cards):
            label = card.get("label", "")
            hand_type = PLANET_HAND_MAP.get(label)
            if not hand_type:
                continue

            # Base score: balance of playability and scaling potential
            score = HAND_VALUE.get(hand_type, 1.0)

            # Strategy is the dominant factor — affinity from jokers
            affinity = strat.hand_affinity(hand_type)
            if affinity > 0:
                score += affinity * 10

            # Constellation: every planet used = +0.1 xmult, even off-strategy
            if has_constellation and affinity == 0:
                score = max(score, 8.0)  # guarantee pick over skip

            # Level bonus: compound growth on already-leveled types
            level_info = hand_levels.get(hand_type, {})
            current_level = level_info.get("level", 1)
            if current_level > 1:
                score *= 1.2 ** (current_level - 1)

            if score > best_score:
                best_score = score
                best_idx = i

        label = cards[best_idx].get("label", "?")
        hand_type = PLANET_HAND_MAP.get(label, "?")
        affinity = strat.hand_affinity(hand_type)

        # Log planets not chosen
        passed = []
        for i, card in enumerate(cards):
            if i == best_idx:
                continue
            cl = card.get("label", "?")
            cht = PLANET_HAND_MAP.get(cl)
            if cht:
                ca = strat.hand_affinity(cht)
                passed.append(f"{cl}({cht}, aff={ca:.0f})")
        if passed:
            log.info("Passed on planets: %s", ", ".join(passed))

        return PackAction(
            card_index=best_idx,
            reason=f"planet: {label} (levels {hand_type}, affinity={affinity:.0f})",
        )


class PickFromBuffoonPack:
    """Pick the joker with the best scoring effect from a Buffoon pack."""
    name = "pick_from_buffoon_pack"

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        pack = state.get("pack", {})
        cards = pack.get("cards", [])
        joker_slots = state.get("jokers", {})

        if not cards:
            return None

        # Only handle Joker/Buffoon packs
        if not any(c.get("set") == "JOKER" for c in cards):
            return None

        # Can't pick jokers with full slots — skip the pack
        if joker_slots.get("count", 0) >= joker_slots.get("limit", 5):
            return PackAction(card_index=None, reason="skip buffoon pack (joker slots full)")

        owned_jokers = joker_slots.get("cards", [])
        owned_keys = {j.get("key") for j in owned_jokers}
        hand_levels = state.get("hands", {})
        ante = state.get("ante_num", 1)
        joker_limit = joker_slots.get("limit", 5)
        strat = compute_strategy(owned_jokers, hand_levels)
        has_madness = "j_madness" in owned_keys

        best_idx = 0
        best_score = -1.0

        from balatro_bot.scaling import check_anti_synergy
        for i, card in enumerate(cards):
            key = card.get("key", "")

            # Madness interaction (bidirectional)
            if has_madness and key in SCALING_JOKERS:
                continue
            if key == "j_madness" and owned_keys & SCALING_JOKERS:
                continue

            # General anti-synergy check
            if check_anti_synergy(key, owned_keys):
                continue

            from balatro_bot.joker_valuation import evaluate_joker_value
            score = evaluate_joker_value(
                card, owned_jokers=owned_jokers,
                hand_levels=hand_levels, ante=ante, strategy=strat,
                joker_limit=joker_limit,
            )
            # S-tier jokers get a massive boost
            from balatro_bot.rules.shop import BuyJokersInShop
            if key in BuyJokersInShop.ALWAYS_BUY:
                score = max(score, 10.0)

            if score > best_score:
                best_score = score
                best_idx = i

        label = cards[best_idx].get("label", "?")
        return PackAction(card_index=best_idx, reason=f"buffoon pick: {label} (score={best_score:.1f})")


class PickFromSpectralPack:
    """Pick the best Spectral card from a Spectral pack."""
    name = "pick_from_spectral_pack"

    # Base scores for each Spectral card; conditions applied at runtime
    SPECTRAL_SCORES: dict[str, float] = {
        "c_ectoplasm": 4.5,   # ×1 xmult on joker — massive, but -1 hand size
        "c_ankh":      4.0,   # clone a random joker
        "c_hex":       3.5,   # polychrome on joker (destroys other consumables)
        "c_wraith":    3.0,   # create random Rare joker, sets money $0
        "c_immolate":  2.5,   # destroy 5 cards, gain $20
        "c_familiar":  2.0,   # destroy 1, add 3 enhanced face cards
        "c_grim":      2.0,   # destroy 1, add 2 enhanced aces
        "c_incantation":2.0,  # destroy 1, add 4 enhanced numbers
        "c_deja_vu":   2.0,   # Red Seal (replays card)
        "c_trance":    2.0,   # Blue Seal (planet when held)
        "c_cryptid":   1.5,   # 2 copies of card in deck
        "c_aura":      1.0,   # random edition on a card
        "c_talisman":  1.0,   # Gold Seal ($3 when played)
        "c_medium":    1.0,   # Purple Seal (tarot when discarded)
        "c_sigil":     0.0,   # random suit conversion — skip
        "c_ouija":     0.0,   # permanent -1 hand size — skip
    }

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        pack = state.get("pack", {})
        cards = pack.get("cards", [])
        if not cards:
            return None

        all_spectral = SAFE_SPECTRAL_CONSUMABLES | SPECTRAL_TARGETING.keys()
        if not any(c.get("key", "") in all_spectral for c in cards):
            return None  # not a Spectral pack

        jokers = state.get("jokers", {}).get("cards", [])
        joker_slots = state.get("jokers", {})
        ante = state.get("ante_num", 1)
        hand_levels = state.get("hands", {})
        joker_keys = {j.get("key") for j in jokers}
        strat = compute_strategy(jokers, hand_levels) if jokers else None

        best_idx = None
        best_score = 0.0

        for i, card in enumerate(cards):
            key = card.get("key", "")
            score = self.SPECTRAL_SCORES.get(key, 0.0)
            if score == 0.0:
                continue

            # Apply runtime conditions
            if key in ("c_ankh", "c_hex") and not jokers:
                score = 0.0
            elif key == "c_hex":
                # Nuanced Hex evaluation — considers joker dominance and ante
                score = evaluate_hex(jokers, ante, hand_levels)
            elif key == "c_ankh":
                if joker_slots.get("count", 0) >= joker_slots.get("limit", 5):
                    score = 0.0
            elif key == "c_wraith":
                if joker_slots.get("count", 0) >= joker_slots.get("limit", 5):
                    score = 0.0
            elif key == "c_ectoplasm":
                if not jokers or ante < 3:
                    score = 0.0
                elif joker_keys & SCALING_JOKERS:
                    score += 1.5  # xmult stacks multiplicatively with scaling jokers

            # Joker-aware bonuses for deck-manipulating spectrals
            if score > 0.0 and strat:
                if key == "c_familiar" and strat.has_archetype("face_card"):
                    score += 1.5  # adds face cards to deck
                if key == "c_grim" and strat.rank_affinity("A") > 0:
                    score += 1.0  # adds aces
                if key == "c_incantation":
                    # adds number cards — check if any number ranks have affinity
                    for r in ("2", "3", "4", "5", "6", "7", "8", "9"):
                        if strat.rank_affinity(r) > 0:
                            score += 1.0
                            break
                if key == "c_aura":
                    # random edition — more valuable with uneditioned jokers
                    uneditioned = sum(
                        1 for j in jokers
                        if not (isinstance(j.get("modifier"), dict) and j["modifier"].get("edition"))
                    )
                    if uneditioned >= 3:
                        score += 1.0

            if score > best_score:
                best_score = score
                best_idx = i

        if best_idx is None:
            return PackAction(card_index=None, reason="skip spectral pack (nothing useful)")

        key = cards[best_idx].get("key", "")
        label = cards[best_idx].get("label", "?")

        # Targeting spectrals need a target card from the current hand
        if key in SPECTRAL_TARGETING:
            hand_cards = state.get("hand", {}).get("cards", [])
            strat = compute_strategy(jokers, state.get("hands", {}))
            max_count, effect_type, extra = SPECTRAL_TARGETING[key]
            targets, _ = _find_tarot_targets(effect_type, extra, max_count, hand_cards, jokers, strat)
            if not targets:
                return PackAction(card_index=None, reason=f"skip spectral pack ({label} needs target, none available)")
            return PackAction(card_index=best_idx, targets=targets, reason=f"spectral pick: {label} (score={best_score:.1f}) -> targets {targets}")

        return PackAction(card_index=best_idx, reason=f"spectral pick: {label} (score={best_score:.1f})")


class PickBestFromPack:
    """Fallback: pick the first non-targeting card, or skip."""
    name = "pick_best_from_pack"

    # Tarot cards that require target card selection — we can't use these
    # from packs since we don't have target selection logic yet.
    NEEDS_TARGETS = {
        "c_magician", "c_high_priestess", "c_empress", "c_emperor",
        "c_hierophant", "c_lovers", "c_chariot", "c_justice", "c_hermit",
        "c_wheel_of_fortune", "c_strength", "c_hanged_man", "c_death",
        "c_temperance", "c_devil", "c_tower", "c_star", "c_moon", "c_sun",
        "c_judgement", "c_world",
    }

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        pack = state.get("pack", {})
        cards = pack.get("cards", [])
        if not cards:
            return PackAction(card_index=None, reason="skip empty pack")

        # Pick first card that doesn't need targets
        for i, card in enumerate(cards):
            key = card.get("key", "")
            if key not in self.NEEDS_TARGETS:
                return PackAction(card_index=i, reason=f"pick: {card.get('label', '?')}")

        # All cards need targets — skip the pack
        return PackAction(card_index=None, reason="skip pack (all cards need targets)")
