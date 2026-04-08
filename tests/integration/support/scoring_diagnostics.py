"""Scoring infrastructure and diagnostic output for integration tests.

Provides:
- Score estimation via bot internals (build_snapshot, score_snapshot, play_hand_and_score)
- Formatted card/joker display (fmt_card, fmt_joker_ability)
- Detailed scoring dumps matching bot.py's scoring log format
"""

from __future__ import annotations

import os
import sys

_src = os.path.join(os.path.dirname(__file__), "..", "..", "src")
if _src not in sys.path:
    sys.path.insert(0, _src)

from balatrobot.cli.client import APIError
from balatro_bot.domain.scoring.classify import classify_hand, _scoring_cards_for
from balatro_bot.domain.scoring.estimate import score_hand_detailed
from balatro_bot.cards import is_joker_debuffed, _modifier, card_rank, card_suits
from balatro_bot.joker_effects.parsers import _ability, _ab_mult, _ab_chips, _ab_xmult


# ---------------------------------------------------------------------------
# Card / joker formatting
# ---------------------------------------------------------------------------

def fmt_card(c: dict) -> str:
    """Format a card dict like '10H' or 'KS[LUCKY/FOIL]'."""
    rank = card_rank(c) or "?"
    suits = card_suits(c)
    suit_syms = {"HEARTS": "\u2665", "DIAMONDS": "\u2666", "CLUBS": "\u2663", "SPADES": "\u2660"}
    suit = "".join(suit_syms.get(s, s[0]) for s in suits) if suits else "?"
    mod = _modifier(c)
    tags = []
    if isinstance(mod, dict):
        if mod.get("enhancement"):
            tags.append(mod["enhancement"])
        if mod.get("edition"):
            tags.append(mod["edition"])
        if mod.get("seal"):
            tags.append(mod["seal"])
    tag_str = f"[{'/'.join(tags)}]" if tags else ""
    debuf = "*D*" if isinstance(c.get("state", {}), dict) and c["state"].get("debuff") else ""
    return f"{rank}{suit}{tag_str}{debuf}"


def fmt_joker_ability(j: dict) -> str:
    """Dump ALL ability fields for a joker, plus parsed effect text."""
    key = j.get("key", "?")
    ab = _ability(j)
    effect = j.get("value", {}).get("effect", "")
    ab_str = ", ".join(f"{k}={v}" for k, v in sorted(ab.items()) if v not in (0, 0.0, "", None, [], {}))
    parts = [f"{key}"]
    if ab_str:
        parts.append(f"ability={{{ab_str}}}")
    if effect:
        parts.append(f'effect="{effect}"')
    mod = j.get("modifier", j.get("value", {}).get("modifier", {}))
    if isinstance(mod, dict) and mod.get("edition"):
        parts.append(f"edition={mod['edition']}")
    if is_joker_debuffed(j):
        parts.append("DEBUFFED")
    return "  ".join(parts)


# ---------------------------------------------------------------------------
# Diagnostic dumps — matches bot.py _log_played_hand detail level
# ---------------------------------------------------------------------------

def dump_hand_detail(detail: dict, played: list, held: list, scoring: list,
                     jokers: list, state: dict, pre_chips: int, actual: int,
                     prefix: str = "    ") -> None:
    """Print a full scoring breakdown matching scoring_log format."""
    p = prefix
    hand_name = detail["hand_name"]

    played_str = ", ".join(fmt_card(c) for c in played)
    scoring_str = ", ".join(fmt_card(c) for c in scoring)
    held_str = ", ".join(fmt_card(c) for c in held) if held else "(none)"
    print(f"{p}played:  [{played_str}] ({len(played)} cards)")
    print(f"{p}scoring: [{scoring_str}] ({len(scoring)} cards)")
    print(f"{p}held:    [{held_str}] ({len(held)} cards)")

    print(f"{p}hand: {hand_name}  base_chips={detail['base_chips']}  base_mult={detail['base_mult']}")

    for label, chips, mult, xmult in detail.get("card_details", []):
        parts = []
        if chips:
            parts.append(f"+{chips}c")
        if mult:
            parts.append(f"+{mult}m")
        if xmult != 1.0:
            parts.append(f"x{xmult:.2f}")
        if parts:
            print(f"{p}  card {label}: {', '.join(parts)}")

    print(f"{p}pre-joker:  chips={detail['pre_joker_chips']}  mult={detail['pre_joker_mult']:.1f}")

    for entry in detail.get("joker_contributions", []):
        label, dc, dm = entry[0], entry[1], entry[2]
        xm = entry[3] if len(entry) > 3 else 1.0
        parts = []
        if dc:
            parts.append(f"+{dc:.0f}c")
        if xm > 1.01 or xm < 0.99:
            parts.append(f"x{xm:.2f}")
        elif dm:
            parts.append(f"+{dm:.1f}m")
        effect_str = f"  ({', '.join(parts)})" if parts else "  (no effect)"
        print(f"{p}  joker {label}{effect_str}")

    print(f"{p}post-joker: chips={detail['post_joker_chips']}  mult={detail['post_joker_mult']:.1f}")
    print(f"{p}total:      {detail['total']}  =  floor({detail['post_joker_chips']} * {detail['post_joker_mult']:.1f})")
    print(f"{p}actual:     {actual}  (pre_round_chips={pre_chips})")

    diff = actual - detail["total"]
    if diff != 0:
        print(f"{p}*** MISMATCH: {diff:+d} ***")

    print(f"{p}--- joker abilities (pre-play snapshot) ---")
    for j in jokers:
        print(f"{p}  {fmt_joker_ability(j)}")

    rnd = state.get("round", {})
    print(f"{p}--- context ---")
    print(f"{p}  money={state.get('money', '?')}  discards_left={rnd.get('discards_left', '?')}  "
          f"hands_left={rnd.get('hands_left', '?')}  deck_count={state.get('cards', {}).get('count', '?')}")
    print(f"{p}  blind={detail.get('hand_name', '?')}  ante={state.get('ante_num', '?')}")


def dump_joker_diff(pre_jokers: list, post_jokers: list, prefix: str = "    ") -> None:
    """Compare joker ability dicts before and after a play, print changes."""
    p = prefix
    pre_map = {}
    for j in pre_jokers:
        key = j.get("key", "")
        pre_map[key] = dict(_ability(j))

    post_map = {}
    for j in post_jokers:
        key = j.get("key", "")
        post_map[key] = dict(_ability(j))

    changes = []
    for key in pre_map:
        pre_ab = pre_map[key]
        post_ab = post_map.get(key, {})
        if pre_ab != post_ab:
            diffs = []
            all_keys = set(pre_ab) | set(post_ab)
            for k in sorted(all_keys):
                pv = pre_ab.get(k)
                nv = post_ab.get(k)
                if pv != nv:
                    diffs.append(f"{k}: {pv} -> {nv}")
            changes.append(f"{key}: {', '.join(diffs)}")

    for key in post_map:
        if key not in pre_map:
            changes.append(f"{key}: NEW (not in pre-play)")
    for key in pre_map:
        if key not in post_map:
            changes.append(f"{key}: REMOVED (not in post-play)")

    if changes:
        print(f"{p}--- joker ability changes (pre -> post play) ---")
        for c in changes:
            print(f"{p}  {c}")
    else:
        print(f"{p}--- joker abilities: NO CHANGES ---")


# ---------------------------------------------------------------------------
# Scoring infrastructure
# ---------------------------------------------------------------------------

def build_snapshot(state: dict, params: dict, hand_name: str) -> dict:
    """Build a play snapshot exactly like bot.py lines 700-740."""
    hand_cards = state.get("hand", {}).get("cards", [])
    play_indices = set(params.get("cards", []))

    snap_hand_levels = state.get("hands", {})
    snap_blind_name = ""
    boss_disabled = state.get("_boss_disabled", False)
    for b in state.get("blinds", {}).values():
        if isinstance(b, dict) and b.get("status") == "CURRENT":
            snap_blind_name = b.get("name", "")
            if snap_blind_name == "The Flint" and not boss_disabled:
                from balatro_bot.domain.scoring.base import flint_halve_hand_levels
                snap_hand_levels = flint_halve_hand_levels(snap_hand_levels)
            break

    return {
        "played": [hand_cards[i] for i in sorted(params.get("cards", [])) if i < len(hand_cards)],
        "held": [c for j, c in enumerate(hand_cards) if j not in play_indices],
        "jokers": state.get("jokers", {}).get("cards", []),
        "hand_levels": snap_hand_levels,
        "money": state.get("money", 0),
        "discards_left": state.get("round", {}).get("discards_left", 0),
        "hands_left": state.get("round", {}).get("hands_left", 1),
        "joker_limit": state.get("jokers", {}).get("limit", 5),
        "hand_name": hand_name,
        "ancient_suit": state.get("round", {}).get("ancient_suit"),
        "deck_count": state.get("cards", {}).get("count", 0),
        "deck_cards": state.get("cards", {}).get("cards", []),
        "blind_name": snap_blind_name,
        "ante": state.get("ante_num", 1),
    }


def score_snapshot(snapshot: dict) -> tuple[dict, list]:
    """Score a snapshot exactly like bot.py _log_played_hand.

    Returns (detail_dict, scoring_cards).
    """
    played = snapshot["played"]
    hand_name = snapshot["hand_name"]
    if not hand_name:
        hand_name = classify_hand(played)

    joker_keys = {j.get("key") for j in snapshot["jokers"]}
    has_splash = "j_splash" in joker_keys
    four_fingers = "j_four_fingers" in joker_keys
    smeared = "j_smeared" in joker_keys
    scoring = played if has_splash else _scoring_cards_for(
        hand_name, played, four_fingers=four_fingers, smeared=smeared)

    hand_levels = snapshot["hand_levels"]
    blind_name = snapshot.get("blind_name", "")
    if blind_name == "The Arm":
        from balatro_bot.domain.scoring.base import arm_reduce_hand_levels
        hand_levels = arm_reduce_hand_levels(hand_levels)

    detail = score_hand_detailed(
        hand_name, scoring,
        hand_levels=hand_levels,
        jokers=snapshot["jokers"],
        played_cards=played,
        held_cards=snapshot["held"],
        money=snapshot["money"],
        discards_left=snapshot["discards_left"],
        hands_left=snapshot["hands_left"],
        joker_limit=snapshot["joker_limit"],
        ancient_suit=snapshot.get("ancient_suit"),
        deck_count=snapshot.get("deck_count", 0),
        deck_cards=snapshot.get("deck_cards"),
        blind_name=blind_name,
    )
    return detail, scoring


def play_hand_and_score(client, state: dict, play_indices: list[int],
                        blind_name: str = "") -> dict | None:
    """Play cards at indices, compare estimate vs actual, return result dict.

    Returns None if the play call fails.
    """
    hand_cards = state.get("hand", {}).get("cards", [])
    jokers = state.get("jokers", {}).get("cards", [])
    hand_levels = state.get("hands", {})

    played = [hand_cards[i] for i in play_indices if i < len(hand_cards)]
    held = [c for j, c in enumerate(hand_cards) if j not in set(play_indices)]

    hand_name = classify_hand(played)

    joker_keys_set = {j.get("key") for j in jokers if not is_joker_debuffed(j)}
    four_fingers = "j_four_fingers" in joker_keys_set
    smeared = "j_smeared" in joker_keys_set
    scoring = played if "j_splash" in joker_keys_set else _scoring_cards_for(
        hand_name, played, four_fingers=four_fingers, smeared=smeared)

    detail = score_hand_detailed(
        hand_name, scoring,
        hand_levels=hand_levels,
        jokers=jokers,
        played_cards=played,
        held_cards=held,
        money=state.get("money", 0),
        discards_left=state.get("round", {}).get("discards_left", 0),
        hands_left=state.get("round", {}).get("hands_left", 1),
        joker_limit=state.get("jokers", {}).get("limit", 5),
        blind_name=blind_name,
        deck_count=state.get("cards", {}).get("count", 0),
    )

    pre_chips = state.get("round", {}).get("chips", 0)

    try:
        new_state = client.call("play", {"cards": play_indices})
    except APIError as e:
        print(f"    Play failed: {e.message}")
        return None

    post_chips = new_state.get("round", {}).get("chips", 0)
    actual = post_chips - pre_chips
    diff = actual - detail["total"]

    return {
        "hand_name": hand_name,
        "est": detail["total"],
        "actual": actual,
        "diff": diff,
        "detail": detail,
        "played": played,
        "held": held,
        "scoring": scoring,
        "jokers": jokers,
        "pre_chips": pre_chips,
        "state": state,
        "new_state": new_state,
        "state_source": "gamestate",
    }
