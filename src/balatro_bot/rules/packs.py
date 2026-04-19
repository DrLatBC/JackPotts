from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from balatro_bot.actions import PackAction, Action
from balatro_bot.cards import card_rank, card_suit, joker_key
from balatro_bot.constants import (
    NO_TARGET_TAROTS, TARGETING_TAROTS, PLANET_HAND_MAP, PLANET_KEYS,
    SAFE_SPECTRAL_CONSUMABLES, SPECTRAL_TARGETING,
)
from balatro_bot.strategy import compute_strategy, JOKER_HAND_AFFINITY

if TYPE_CHECKING:
    from typing import Any

log = logging.getLogger("balatro_bot")


class SkipPackForRedCard:
    """Skip packs to trigger Red Card's +3 mult scaling.

    Skips when the best pick in the pack is worth less than one +3 mult stack
    (scaled by ante runway). Never skips if the pack contains Planets, Jokers,
    or Black Hole — those are always picked regardless of Red Card.
    """
    name = "skip_pack_for_red_card"

    # Margin: the pick must clearly beat the skip to override.
    _PICK_MARGIN = 1.3

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        from balatro_bot.scaling import red_card_skip_value

        jokers = state.get("jokers", {}).get("cards", [])
        if not any(joker_key(j) == "j_red_card" for j in jokers):
            return None

        pack = state.get("pack", {})
        cards = pack.get("cards", [])
        if not cards:
            return None

        # Hard keeps — skip rule does not fire on these contents
        for c in cards:
            if c.get("set") == "PLANET" or c.get("label", "") in PLANET_HAND_MAP:
                return None
            if c.get("set") == "JOKER":
                return None
            if c.get("key", "") == "c_black_hole":
                return None

        ante = state.get("ante_num", 1)
        skip_value = red_card_skip_value(ante)
        best_pick = _best_pack_pick_value(state, cards, jokers)

        if best_pick > skip_value * self._PICK_MARGIN:
            return None  # let the normal pick rule fire

        return PackAction(
            card_index=None,
            reason=f"skip pack for Red Card (+3 mult, skip={skip_value:.1f} > pick={best_pick:.1f})",
        )


def _best_pack_pick_value(state: dict[str, Any], cards: list[dict], jokers: list[dict]) -> float:
    """Estimate the best achievable pick score for a pack (Arcana/Spectral/Standard).

    Returns 0.0 for pack types we don't expect to skip (caller has already
    bailed out on Celestial/Buffoon via the hard-keep checks).
    """
    from balatro_bot.domain.policy.pack_policy import (
        choose_from_spectral_pack, choose_from_tarot_pack, SPECTRAL_SCORES,
    )

    hand_levels = state.get("hands", {})
    hand_cards = state.get("hand", {}).get("cards", [])
    strat = compute_strategy(jokers, hand_levels)
    ante = state.get("ante_num", 1)

    # Arcana (tarot)
    if any(c.get("key", "") in (set(NO_TARGET_TAROTS) | set(TARGETING_TAROTS)) for c in cards):
        _, best_score, _, _ = choose_from_tarot_pack(cards, state, hand_cards, jokers, strat)
        return best_score

    # Spectral
    if any(c.get("key", "") in SPECTRAL_SCORES for c in cards):
        joker_slots = state.get("jokers", {})
        _, best_score, _, _ = choose_from_spectral_pack(
            cards, jokers, joker_slots, ante, hand_levels, hand_cards, strat,
        )
        return best_score

    # Standard — estimate from editions/enhancements; most picks are modest
    _EDITION = {"POLYCHROME": 8.0, "HOLOGRAPHIC": 4.0, "FOIL": 2.0}
    _ENH = {"STEEL": 5.0, "GLASS": 4.0, "LUCKY": 3.0, "MULT": 3.0,
            "WILD": 2.0, "BONUS": 2.0, "GOLD": 2.0}
    best = 0.0
    for c in cards:
        score = 0.0
        mod = c.get("modifier") if isinstance(c.get("modifier"), dict) else {}
        score += _EDITION.get(mod.get("edition", ""), 0.0)
        score += _ENH.get(mod.get("enhancement", ""), 0.0)
        if score > best:
            best = score
    return best


class PickFromTarotPack:
    """Pick the best Tarot card from an Arcana pack using dynamic scoring."""
    name = "pick_from_tarot_pack"

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        from balatro_bot.domain.policy.pack_policy import choose_from_tarot_pack

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

        best_idx, best_score, reason, targets = choose_from_tarot_pack(
            cards, state, hand_cards, jokers, strat,
        )

        if best_idx is not None:
            if targets:
                return PackAction(card_index=best_idx, targets=targets, reason=reason)
            return PackAction(card_index=best_idx, reason=reason)

        return PackAction(card_index=None, reason=reason)


class PickFromPlanetPack:
    """Pick the planet card that best synergizes with our strategy."""
    name = "pick_from_planet_pack"

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        from balatro_bot.domain.policy.pack_policy import choose_from_planet_pack

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

        jokers = state.get("jokers", {}).get("cards", [])
        joker_keys = {joker_key(j) for j in jokers}
        hand_levels = state.get("hands", {})
        strat = compute_strategy(jokers, hand_levels)

        best_idx, best_score, reason = choose_from_planet_pack(
            cards, strat, hand_levels, joker_keys,
        )

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

        return PackAction(card_index=best_idx, reason=reason)


class PickFromBuffoonPack:
    """Pick the joker with the best scoring effect from a Buffoon pack."""
    name = "pick_from_buffoon_pack"

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        from balatro_bot.domain.policy.pack_policy import choose_from_buffoon_pack

        pack = state.get("pack", {})
        cards = pack.get("cards", [])
        joker_slots = state.get("jokers", {})

        if not cards:
            return None

        # Only handle Joker/Buffoon packs
        if not any(c.get("set") == "JOKER" for c in cards):
            return None

        # Can't pick jokers with full slots — unless a Negative joker is available
        if joker_slots.get("count", 0) >= joker_slots.get("limit", 5):
            from balatro_bot.domain.policy.shop import _is_negative
            if not any(_is_negative(c) for c in cards):
                return PackAction(card_index=None, reason="skip buffoon pack (joker slots full)")

        owned_jokers = joker_slots.get("cards", [])
        hand_levels = state.get("hands", {})
        ante = state.get("ante_num", 1)
        joker_limit = joker_slots.get("limit", 5)
        strat = compute_strategy(owned_jokers, hand_levels)

        from balatro_bot.domain.policy.shop import ALWAYS_BUY, _get_deck_profile
        blind_name = next(
            (b.get("name") for b in state.get("blinds", {}).values()
             if isinstance(b, dict) and b.get("status") == "CURRENT"),
            None,
        )
        best_idx, best_score, reason = choose_from_buffoon_pack(
            cards, owned_jokers, hand_levels, ante, joker_limit, strat,
            always_buy_keys=ALWAYS_BUY,
            deck_profile=_get_deck_profile(state),
            blind_name=blind_name,
        )

        return PackAction(card_index=best_idx, reason=reason)


class PickFromSpectralPack:
    """Pick the best Spectral card from a Spectral pack."""
    name = "pick_from_spectral_pack"

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        from balatro_bot.domain.policy.pack_policy import choose_from_spectral_pack

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
        hand_cards = state.get("hand", {}).get("cards", [])
        strat = compute_strategy(jokers, hand_levels) if jokers else None

        best_idx, best_score, reason, targets = choose_from_spectral_pack(
            cards, jokers, joker_slots, ante, hand_levels, hand_cards, strat,
        )

        if best_idx is None:
            return PackAction(card_index=None, reason=reason)

        if targets:
            return PackAction(card_index=best_idx, targets=targets, reason=reason)

        return PackAction(card_index=best_idx, reason=reason)


class PickFromStandardPack:
    """Evaluate Standard pack cards against strategy; pick or skip."""
    name = "pick_from_standard_pack"

    # Minimum score for a card to be worth picking (vs diluting the deck)
    _PICK_THRESHOLD = 2.0

    # Bonus for editions/enhancements/seals on Standard pack cards
    _EDITION_BONUS = {"POLYCHROME": 8.0, "HOLOGRAPHIC": 4.0, "FOIL": 2.0}
    _ENHANCEMENT_BONUS = {
        "STEEL": 5.0, "GLASS": 4.0, "LUCKY": 3.0, "MULT": 3.0,
        "WILD": 2.0, "BONUS": 2.0, "GOLD": 2.0,
    }
    # Red retrigger is the strongest seal (effectively doubles the card's
    # contribution every play). Gold $3/play is solid economy. Blue/Purple
    # are conditional (held-at-end / discarded) but still free value.
    _SEAL_BONUS = {"RED": 6.0, "GOLD": 4.0, "BLUE": 3.0, "PURPLE": 3.0}

    def evaluate(self, state: dict[str, Any]) -> Action | None:
        pack = state.get("pack", {})
        cards = pack.get("cards", [])
        if not cards:
            return None

        # Only handle Standard packs — cards should be playing cards
        if not all(c.get("set") in ("DEFAULT", "PLAYING_CARD", None) for c in cards):
            return None

        jokers = state.get("jokers", {}).get("cards", [])
        hand_levels = state.get("hands", {})
        strat = compute_strategy(jokers, hand_levels)

        best_idx = None
        best_score = -1.0

        for i, card in enumerate(cards):
            score = 0.0

            # Suit affinity
            suit = card_suit(card)
            if suit and strat.preferred_suits:
                score += strat.suit_affinity(suit) * 1.5

            # Rank affinity
            rank = card_rank(card)
            if rank:
                score += strat.rank_affinity(rank) * 1.0

            # Edition/enhancement/seal bonuses
            mod = card.get("modifier", {})
            if isinstance(mod, dict):
                edition = mod.get("edition")
                if edition:
                    score += self._EDITION_BONUS.get(edition.upper(), 0.0)
                enhancement = mod.get("enhancement")
                if enhancement:
                    score += self._ENHANCEMENT_BONUS.get(enhancement.upper(), 0.0)
                seal = mod.get("seal")
                if seal:
                    score += self._SEAL_BONUS.get(seal.upper(), 0.0)

            if score > best_score:
                best_score = score
                best_idx = i

        if best_idx is not None and best_score >= self._PICK_THRESHOLD:
            c = cards[best_idx]
            label = c.get("label", "?")
            log.info("Standard pack: pick %s (score=%.1f)", label, best_score)
            return PackAction(
                card_index=best_idx,
                reason=f"standard pack: pick {label} (value={best_score:.1f})",
            )

        return PackAction(card_index=None, reason="skip standard pack (no strategic value)")


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
