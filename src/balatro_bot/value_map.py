"""Generate a comprehensive value map of every joker under canonical scenarios.

Feeds the JackPotts dashboard `/value-map` page. Called by supervisor at
batch start, also runnable standalone: ``python -m balatro_bot.value_map``.

The output is a list of rows; each row contains the joker metadata plus a
``values`` dict keyed by scenario name. Pushed to the dashboard as-is.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from balatro_bot.domain.models.card import Card  # noqa: F401 — warm import cycle
from balatro_bot.domain.models.deck_profile import DeckProfile
from balatro_bot.domain.policy.shop_valuation import evaluate_joker_value
from balatro_bot.joker_registry import JOKERS, RARITY_LABEL
from balatro_bot.scaling import ALL_SCALING
from balatro_bot.strategy import compute_strategy

if TYPE_CHECKING:
    from balatro_bot.strategy import Strategy


# ---------------------------------------------------------------------------
# Canonical scenarios
# ---------------------------------------------------------------------------

_VANILLA_DECK = DeckProfile(
    total_cards=52,
    rank_counts={r: 4 for r in "23456789TJQKA"},
    suit_counts={s: 13 for s in "HDCS"},
)

_HAND_LEVELS = {h: {"level": 1, "chips": c, "mult": m} for h, c, m in [
    ("High Card", 5, 1), ("Pair", 10, 2), ("Two Pair", 20, 2),
    ("Three of a Kind", 30, 3), ("Straight", 30, 4), ("Flush", 35, 4),
    ("Full House", 40, 4), ("Four of a Kind", 60, 7),
    ("Straight Flush", 100, 8),
]}


def _mk(key: str, effect: str = "", rarity: int = 1, cost: int = 5) -> dict:
    """Build a minimal joker dict shaped like a real shop card."""
    return {
        "key": key, "label": key,
        "value": {"effect": effect, "rarity": rarity},
        "cost": {"buy": cost, "sell": max(1, cost // 2)},
    }


# Pre-built owned rosters for each scenario. Kept deliberately small and
# archetypal — each represents a recognizable build state the bot reaches.
_BANNER = _mk("j_banner", "+30 Chips")
_JOKER = _mk("j_joker", "+4 Mult")
_TRIBE = _mk("j_tribe", "X2 Mult if hand contains a Flush", rarity=2)
_PHOTO = _mk("j_photograph", "First played face card gives X2 Mult", rarity=2)
_BLOODSTONE = _mk("j_bloodstone",
                  "1 in 2 Hearts X1.5 Mult (Currently X1.5)", rarity=2)
_SLY = _mk("j_sly", "+50 Chips if hand contains a Pair")
_JOLLY = _mk("j_jolly", "+8 Mult if hand contains a Pair")
_ZANY = _mk("j_zany", "+12 Mult if hand contains Three of a Kind", rarity=2)
_CRAZY = _mk("j_crazy", "+12 Mult if hand contains a Straight", rarity=2)
_ABSTRACT = _mk("j_abstract", "+3 Mult per Joker")
_TRIBOULET = _mk("j_triboulet", "Kings and Queens give X2 Mult", rarity=4)
_SMILEY = _mk("j_smiley", "Face cards give +5 Mult")
_SCARY = _mk("j_scary_face", "Face cards give +30 Chips")
_PAREIDOLIA = _mk("j_pareidolia", "All cards are considered face cards", rarity=2)
_DROLL = _mk("j_droll", "+10 Mult if hand contains a Flush")
_FOUR_FINGERS = _mk("j_four_fingers", "All Flushes and Straights can be made with 4 cards", rarity=2)

# ---------------------------------------------------------------------------
# Archetypes — representative roster states the bot actually reaches.
# Each evaluated at every ante so the dashboard can show how a joker's
# value evolves across the run for a given build.
# ---------------------------------------------------------------------------

ARCHETYPES: list[tuple[str, list[dict]]] = [
    ("empty",             []),
    ("chip_1j",           [_BANNER]),
    ("chip_mult_2j",      [_BANNER, _JOKER]),
    ("triple_3j",         [_BANNER, _JOKER, _TRIBE]),
    ("pair_2j",           [_SLY, _JOLLY]),
    ("pair_3j",           [_BANNER, _SLY, _JOLLY]),
    ("pair_full_5j",      [_BANNER, _SLY, _JOLLY, _ZANY, _ABSTRACT]),
    ("flush_3j",          [_BANNER, _JOKER, _TRIBE]),
    ("flush_5j",          [_BANNER, _JOKER, _TRIBE, _DROLL, _FOUR_FINGERS]),
    ("flush_scorers_5j",  [_BANNER, _JOKER, _TRIBE, _PHOTO, _BLOODSTONE]),
    ("straight_3j",       [_BANNER, _JOKER, _CRAZY]),
    ("face_3j",           [_PHOTO, _SMILEY, _SCARY]),
    ("face_5j",           [_PHOTO, _SMILEY, _SCARY, _TRIBOULET, _PAREIDOLIA]),
]

ANTES: list[int] = [1, 2, 3, 4, 5, 6, 7, 8]


def _scenario_label(archetype: str, ante: int) -> str:
    return f"{archetype}_a{ante}"


# Flat scenario list — (label, owned_jokers, ante) for the evaluator loop.
SCENARIOS: list[tuple[str, list[dict], int]] = [
    (_scenario_label(name, ante), owned, ante)
    for name, owned in ARCHETYPES
    for ante in ANTES
]


# ---------------------------------------------------------------------------
# Effect-text synthesis
# ---------------------------------------------------------------------------

# Starting "(Currently X…)" baseline per scaling joker. Jokers that have a
# non-X1.0 fresh value (Glass at X1.5, Ramen at X2, static-xmult scalers)
# read wrong if synthesized at X1.0.
_SCALING_START_XMULT: dict[str, float] = {
    "j_glass": 1.5,
    "j_ramen": 2.0,
    "j_cavendish": 3.0,
}


def _synthesize_effect(key: str, effect_hint: str) -> str:
    """Produce plausible effect text for parse_effect_value().

    Scaling jokers need a ``(Currently …)`` anchor so the parser reads them
    at their baseline. Non-scaling jokers fall through to the scoring sim
    via the registry dispatch, so empty text is fine.
    """
    if key in ALL_SCALING:
        start = _SCALING_START_XMULT.get(key, 1.0)
        return f"{effect_hint} (Currently X{start})".strip()
    return effect_hint


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

def build_value_map() -> list[dict[str, Any]]:
    """Evaluate every joker across every scenario and return rows."""
    rows: list[dict[str, Any]] = []
    for entry in JOKERS:
        key = entry["key"]
        cand = _mk(
            key,
            effect=_synthesize_effect(key, entry["effect_hint"]),
            rarity=entry["rarity"],
            cost=entry["cost"],
        )
        values: dict[str, float] = {}
        for label, owned, ante in SCENARIOS:
            strategy: Strategy = compute_strategy(owned, _HAND_LEVELS)
            try:
                v = evaluate_joker_value(
                    cand, owned, _HAND_LEVELS, ante,
                    strategy=strategy, deck_profile=_VANILLA_DECK,
                )
            except Exception:  # defensive — never let one joker break the map
                v = 0.0
            values[label] = round(v, 2)
        rows.append({
            "key": key,
            "name": entry["name"],
            "rarity": entry["rarity"],
            "rarity_label": RARITY_LABEL.get(entry["rarity"], "?"),
            "cost": entry["cost"],
            "effect_hint": entry["effect_hint"],
            "values": values,
        })
    return rows


def scenario_labels() -> list[str]:
    return [label for label, _, _ in SCENARIOS]


def archetype_names() -> list[str]:
    return [name for name, _ in ARCHETYPES]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Dump or push the joker value map.")
    parser.add_argument("--batch-id", type=int,
                        help="Dashboard batch id. Required with --push.")
    parser.add_argument("--push", action="store_true",
                        help="POST the map to the dashboard (requires JACKPOTTS_URL/KEY).")
    parser.add_argument("--out", help="Write JSON to this path instead of stdout.")
    args = parser.parse_args()

    rows = build_value_map()
    payload = {
        "scenarios": scenario_labels(),
        "archetypes": archetype_names(),
        "antes": ANTES,
        "rows": rows,
    }

    if args.push:
        if args.batch_id is None:
            parser.error("--push requires --batch-id")
        from balatro_bot.dashboard_client import post_value_map
        post_value_map(args.batch_id, payload)
        print(f"Pushed {len(rows)} joker rows × {len(scenario_labels())} scenarios "
              f"for batch {args.batch_id}.")
    elif args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        print(f"Wrote {args.out}")
    else:
        print(json.dumps(payload, indent=2))
