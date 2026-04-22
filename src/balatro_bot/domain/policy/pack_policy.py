"""Pack-phase policy functions — pure decision logic extracted from rules.

Contains scoring and pick logic for pack decisions (Tarot, Planet, Buffoon, Spectral).
Rules in rules/packs.py become thin wrappers around these.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from balatro_bot.cards import joker_key
from balatro_bot.constants import (
    NO_TARGET_TAROTS, TARGETING_TAROTS, PLANET_HAND_MAP, PLANET_KEYS,
    SCALING_JOKERS, SAFE_SPECTRAL_CONSUMABLES, SPECTRAL_TARGETING,
)

if TYPE_CHECKING:
    from typing import Any
    from balatro_bot.domain.models.deck_profile import DeckProfile
    from balatro_bot.strategy import Strategy

log = logging.getLogger("balatro_bot")

# ---------------------------------------------------------------------------
# Pack-pick tuning constants
# ---------------------------------------------------------------------------

# Planet cards
_BLACK_HOLE_PACK_PRIORITY = 999.0  # Black Hole always picked first
_AFFINITY_MULTIPLIER = 10          # weight of strategy affinity in planet score
_CONSTELLATION_GUARANTEE = 8.0     # min score when Constellation is owned (always pick)
_LEVEL_BONUS_BASE = 1.2            # compound bonus exponent per existing level

# Buffoon (joker) packs
_S_TIER_MIN_SCORE = 10.0           # floor score for ALWAYS_BUY jokers in packs
_NEGATIVE_MIN_SCORE = 10.0         # floor score for Negative edition (free slot)

# Spectral synergy bonuses
_ECTOPLASM_SCALING_BONUS = 1.5     # bonus when scaling jokers owned
_FAMILIAR_FACE_BONUS = 1.5         # bonus when face-card archetype active
_GRIM_ACE_BONUS = 1.0              # bonus when ace affinity > 0
_INCANTATION_NUMBER_BONUS = 1.0    # bonus when any number-rank affinity > 0
_AURA_EDITION_BONUS = 1.0          # bonus when 3+ uneditioned jokers
_AURA_MIN_UNEDITIONED = 3          # threshold for Aura bonus


# ---------------------------------------------------------------------------
# Planet pack scoring (extracted from PickFromPlanetPack)
# ---------------------------------------------------------------------------

# Balance of playability (how often you get this hand) and scaling
# potential (how well the base chips×mult grows with levels).
HAND_VALUE: dict[str, float] = {
    "Two Pair": 8, "Pair": 7,          # most common hands — best default
    "Full House": 6, "Flush": 6, "Straight": 6,
    "Three of a Kind": 5,
    "High Card": 4, "Four of a Kind": 4,
    "Straight Flush": 2, "Five of a Kind": 2,
    "Flush House": 2, "Flush Five": 2,
}


def score_planet_card(
    card: dict,
    strat: Strategy,
    hand_levels: dict,
    joker_keys: set[str],
) -> float:
    """Score a planet card for pack pick decisions.

    Returns a float score — higher is better. 0.0 means skip.
    """
    label = card.get("label", "")

    # Black Hole — always top priority
    if card.get("key", "") == "c_black_hole" or label == "Black Hole":
        return _BLACK_HOLE_PACK_PRIORITY

    hand_type = PLANET_HAND_MAP.get(label)
    if not hand_type:
        return 0.0

    # Base score: balance of playability and scaling potential
    score = HAND_VALUE.get(hand_type, 1.0)

    # Strategy is the dominant factor — affinity from jokers
    affinity = strat.hand_affinity(hand_type)
    if affinity > 0:
        score += affinity * _AFFINITY_MULTIPLIER

    # Constellation: every planet used = +0.1 xmult, even off-strategy
    has_constellation = "j_constellation" in joker_keys
    if has_constellation and affinity == 0:
        score = max(score, _CONSTELLATION_GUARANTEE)  # guarantee pick over skip

    # Level bonus: compound growth on already-leveled types
    level_info = hand_levels.get(hand_type, {})
    current_level = level_info.get("level", 1)
    if current_level > 1:
        score *= _LEVEL_BONUS_BASE ** (current_level - 1)

    return score


def choose_from_planet_pack(
    cards: list[dict],
    strat: Strategy,
    hand_levels: dict,
    joker_keys: set[str],
) -> tuple[int, float, str]:
    """Pick the best planet from a pack.

    Returns (best_index, best_score, reason_string).
    """
    best_idx = 0
    best_score = -1.0

    for i, card in enumerate(cards):
        score = score_planet_card(card, strat, hand_levels, joker_keys)
        if score > best_score:
            best_score = score
            best_idx = i

    label = cards[best_idx].get("label", "?")
    hand_type = PLANET_HAND_MAP.get(label, "?")
    affinity = strat.hand_affinity(hand_type)
    reason = f"planet: {label} (levels {hand_type}, affinity={affinity:.0f})"
    return (best_idx, best_score, reason)


# ---------------------------------------------------------------------------
# Buffoon pack scoring (extracted from PickFromBuffoonPack)
# ---------------------------------------------------------------------------


def choose_from_buffoon_pack(
    cards: list[dict],
    owned_jokers: list[dict],
    hand_levels: dict,
    ante: int,
    joker_limit: int,
    strat: Strategy,
    always_buy_keys: set[str],
    deck_profile: DeckProfile | None = None,
    blind_name: str | None = None,
) -> tuple[int, float, str]:
    """Pick the best joker from a Buffoon pack.

    Returns (best_index, best_score, reason_string).
    """
    from balatro_bot.domain.policy.shop_valuation import evaluate_joker_value
    from balatro_bot.scaling import check_anti_synergy

    from balatro_bot.domain.policy.shop import _is_negative

    owned_keys = {joker_key(j) for j in owned_jokers}
    slots_full = len(owned_jokers) >= joker_limit

    best_idx = 0
    best_score = -1.0

    for i, card in enumerate(cards):
        key = card.get("key", "")

        # Non-Negative jokers can't be picked when slots are full
        if slots_full and not _is_negative(card):
            continue

        # Don't add Madness onto an existing scaler roster — it will eat them.
        # (Buying scalers *into* a Madness roster is fine — they're fodder.)
        if key == "j_madness" and owned_keys & SCALING_JOKERS:
            continue

        # General anti-synergy check
        if check_anti_synergy(key, owned_keys):
            continue

        score = evaluate_joker_value(
            card, owned_jokers=owned_jokers,
            hand_levels=hand_levels, ante=ante, strategy=strat,
            joker_limit=joker_limit, deck_profile=deck_profile,
            blind_name=blind_name,
        )
        # S-tier jokers get a massive boost
        if key in always_buy_keys:
            score = max(score, _S_TIER_MIN_SCORE)

        # Negative jokers in packs are free — always take them
        if _is_negative(card):
            score = max(score, _NEGATIVE_MIN_SCORE)

        if score > best_score:
            best_score = score
            best_idx = i

    label = cards[best_idx].get("label", "?")
    reason = f"buffoon pick: {label} (score={best_score:.1f})"
    return (best_idx, best_score, reason)


# ---------------------------------------------------------------------------
# Spectral pack scoring (extracted from PickFromSpectralPack)
# ---------------------------------------------------------------------------

# Base scores for each Spectral card; conditions applied at runtime
SPECTRAL_SCORES: dict[str, float] = {
    "c_ectoplasm": 4.5,   # ×1 xmult on joker — massive, but -1 hand size
    "c_ankh":      4.0,   # clone a random joker
    "c_hex":       3.5,   # polychrome on joker (destroys other consumables)
    "c_wraith":    3.0,   # create random Rare joker, sets money $0
    "c_immolate":  2.5,   # destroy 5 cards, gain $20
    "c_familiar":  2.0,   # destroy 1, add 3 enhanced face cards
    "c_grim":      2.0,   # destroy 1, add 2 enhanced aces
    "c_incantation": 2.0, # destroy 1, add 4 enhanced numbers
    "c_deja_vu":   2.0,   # Red Seal (replays card)
    "c_trance":    2.0,   # Blue Seal (planet when held)
    "c_cryptid":   1.5,   # 2 copies of card in deck
    "c_aura":      1.0,   # random edition on a card
    "c_talisman":  1.0,   # Gold Seal ($3 when played)
    "c_medium":    1.0,   # Purple Seal (tarot when discarded)
    "c_sigil":     0.0,   # random suit conversion — skip
    "c_ouija":     0.0,   # permanent -1 hand size — skip
}


def score_spectral_card(
    card: dict,
    jokers: list[dict],
    joker_slots: dict,
    ante: int,
    hand_levels: dict,
    strat: Strategy | None,
) -> float:
    """Score a spectral card for pack pick decisions.

    Returns a float score — higher is better. 0.0 means skip.
    """
    from balatro_bot.domain.policy.consumable_policy import evaluate_hex

    key = card.get("key", "")
    score = SPECTRAL_SCORES.get(key, 0.0)
    if score == 0.0:
        return 0.0

    joker_keys = {joker_key(j) for j in jokers}
    # Hex/Ectoplasm pick a random *editionless* joker. If every joker already
    # has an edition (Foil/Holo/Poly/Negative), the game silently no-ops.
    editionless_jokers = [
        j for j in jokers
        if not (isinstance(j.get("modifier"), dict) and j["modifier"].get("edition"))
    ]

    # Apply runtime conditions
    if key in ("c_ankh", "c_hex") and not jokers:
        score = 0.0
    elif key == "c_hex":
        if not editionless_jokers:
            score = 0.0
        else:
            # Nuanced Hex evaluation — considers joker dominance and ante
            score = evaluate_hex(jokers, ante, hand_levels)
    elif key == "c_ankh":
        if joker_slots.get("count", 0) >= joker_slots.get("limit", 5):
            score = 0.0
    elif key == "c_wraith":
        if joker_slots.get("count", 0) >= joker_slots.get("limit", 5):
            score = 0.0
    elif key == "c_ectoplasm":
        if not editionless_jokers or ante < 3:
            score = 0.0
        elif joker_keys & SCALING_JOKERS:
            score += _ECTOPLASM_SCALING_BONUS

    # Joker-aware bonuses for deck-manipulating spectrals
    if score > 0.0 and strat:
        if key == "c_familiar" and strat.has_archetype("face_card"):
            score += _FAMILIAR_FACE_BONUS
        if key == "c_grim" and strat.rank_affinity("A") > 0:
            score += _GRIM_ACE_BONUS
        if key == "c_incantation":
            # adds number cards — check if any number ranks have affinity
            for r in ("2", "3", "4", "5", "6", "7", "8", "9"):
                if strat.rank_affinity(r) > 0:
                    score += _INCANTATION_NUMBER_BONUS
                    break
        if key == "c_aura":
            # random edition — more valuable with uneditioned jokers
            uneditioned = sum(
                1 for j in jokers
                if not (isinstance(j.get("modifier"), dict) and j["modifier"].get("edition"))
            )
            if uneditioned >= _AURA_MIN_UNEDITIONED:
                score += _AURA_EDITION_BONUS

    return score


def choose_from_spectral_pack(
    cards: list[dict],
    jokers: list[dict],
    joker_slots: dict,
    ante: int,
    hand_levels: dict,
    hand_cards: list[dict],
    strat: Strategy | None,
) -> tuple[int | None, float, str, list[int] | None]:
    """Pick the best spectral from a pack.

    Returns (best_index_or_None, best_score, reason_string, targets_or_None).
    """
    from balatro_bot.rules._helpers import _find_tarot_targets
    from balatro_bot.strategy import compute_strategy

    best_idx = None
    best_score = 0.0
    # Familiar/Grim/Incantation need ≥2 hand cards (one gets destroyed,
    # the others remain). Skip these if hand too small.
    random_destroy = {"c_familiar", "c_grim", "c_incantation", "c_immolate"}

    for i, card in enumerate(cards):
        key = card.get("key", "")
        if key in random_destroy and len(hand_cards) < 2:
            continue
        score = score_spectral_card(card, jokers, joker_slots, ante, hand_levels, strat)
        if score > best_score:
            best_score = score
            best_idx = i

    if best_idx is None:
        return (None, 0.0, "skip spectral pack (nothing useful)", None)

    key = cards[best_idx].get("key", "")
    label = cards[best_idx].get("label", "?")

    # Targeting spectrals need a target card from the current hand
    if key in SPECTRAL_TARGETING:
        pack_strat = compute_strategy(jokers, hand_levels)
        max_count, effect_type, extra = SPECTRAL_TARGETING[key]
        targets, _ = _find_tarot_targets(effect_type, extra, max_count, hand_cards, jokers, pack_strat)
        if not targets:
            return (None, 0.0, f"skip spectral pack ({label} needs target, none available)", None)
        return (best_idx, best_score, f"spectral pick: {label} (score={best_score:.1f}) -> targets {targets}", targets)

    return (best_idx, best_score, f"spectral pick: {label} (score={best_score:.1f})", None)


# ---------------------------------------------------------------------------
# Tarot pack scoring (extracted from PickFromTarotPack)
# ---------------------------------------------------------------------------


def choose_from_tarot_pack(
    cards: list[dict],
    state: dict,
    hand_cards: list[dict],
    jokers: list[dict],
    strat: Strategy,
) -> tuple[int | None, float, str, list[int] | None]:
    """Pick the best tarot from a pack.

    Returns (best_index_or_None, best_score, reason_string, targets_or_None).
    """
    from balatro_bot.domain.policy.consumable_policy import score_consumable
    from balatro_bot.rules._helpers import _find_tarot_targets

    best_idx = None
    best_score = 0.0
    best_targets: list[int] | None = None
    known_tarots = set(NO_TARGET_TAROTS) | set(TARGETING_TAROTS)

    for i, card in enumerate(cards):
        key = card.get("key", "")
        # Skip non-tarots outright — mixed packs used to let a spectral at
        # score 0 win here and the bot would then fire pack({card: i})
        # without targets, wedging the mod's pack-selection guard.
        if key not in known_tarots:
            continue
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
        if best_targets:
            return (best_idx, best_score,
                f"tarot: {label} (value={best_score:.1f}) -> targets {best_targets}",
                best_targets)
        return (best_idx, best_score,
            f"tarot: {label} (value={best_score:.1f})",
            None)

    return (None, 0.0, "skip tarot pack (nothing usable)", None)
