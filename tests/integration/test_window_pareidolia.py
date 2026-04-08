"""Integration tests: The Window boss debuff + joker interactions, and Pareidolia face card effects.

Bug A — The Window + Ride the Bus:
    When The Window debuffs a scoring card (e.g. Qd), the bot's Ride the Bus
    still sees Q as a face card and resets to 0. But in the game, debuffed
    cards don't count as face cards for Ride the Bus's check — so the +mult
    should still fire.

Bug B — Pareidolia + Smiley Face + Photograph + Baron:
    Pareidolia makes ALL cards count as face cards. Smiley Face gives +5 per
    face card scored, Photograph gives x2 for the first face card scored.
    Baron gives x1.5 per held King (rank-specific, not face-generic).

Requires the `set({debuff: true})` API addition to balatrobot's set.lua
endpoint, which re-applies Blind:debuff_card() to all hand cards after
injection via add().

Usage:
    python tests/integration/test_window_pareidolia.py --start-server
    python tests/integration/test_window_pareidolia.py --port 12346
"""

import argparse
import logging
import math
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
_venv_sp = os.path.join(os.path.dirname(__file__), "..", "..", ".venv", "Lib", "site-packages")
if os.path.isdir(_venv_sp) and _venv_sp not in sys.path:
    sys.path.insert(0, os.path.abspath(_venv_sp))

from balatrobot.cli.client import BalatroClient, APIError
from balatro_bot.domain.scoring.classify import classify_hand, _scoring_cards_for
from balatro_bot.domain.scoring.estimate import score_hand_detailed
from balatro_bot.domain.scoring.base import flint_halve_hand_levels
from balatro_bot.cards import card_chip_value, _modifier, is_debuffed, card_rank
from balatro_bot.joker_effects.parsers import parse_effect_value, _ability, _ab_xmult
from balatro_bot.constants import FACE_RANKS

# Lazy-imported: harness eagerly validates config paths at import time.
# from harness import TEST_PORT, ensure_server, stop_server


# =========================================================================
# Helpers
# =========================================================================

def wait_for_state(client, target_states, max_tries=30):
    state = {}
    for _ in range(max_tries):
        state = client.call("gamestate")
        gs = state.get("state", "")
        if gs in target_states:
            return state
        if gs == "BLIND_SELECT":
            client.call("select")
            time.sleep(0.3)
            continue
        if gs == "SHOP":
            client.call("next_round")
            time.sleep(0.3)
            continue
        if gs in ("HAND_PLAYED", "DRAW_TO_HAND", "NEW_ROUND", "ROUND_EVAL"):
            time.sleep(0.3)
            continue
        time.sleep(0.3)
    raise TimeoutError(f"Never reached {target_states}, stuck in {state.get('state')}")


def get_current_blind(state):
    for b in state.get("blinds", {}).values():
        if isinstance(b, dict) and b.get("status") == "CURRENT":
            return b.get("name", ""), b.get("key", "")
    return "", ""


def _beat_blind_fast(client, state):
    try:
        client.call("set", {"chips": 999999})
    except APIError:
        pass
    hc = state.get("hand", {}).get("cards", [])
    if hc:
        client.call("play", {"cards": list(range(min(5, len(hc))))})
    time.sleep(0.5)


def _is_boss_blind_select(state):
    blinds = state.get("blinds", {})
    small_status = blinds.get("small", {}).get("status", "") if isinstance(blinds.get("small"), dict) else ""
    big_status = blinds.get("big", {}).get("status", "") if isinstance(blinds.get("big"), dict) else ""
    return small_status == "DEFEATED" and big_status == "DEFEATED"


def _advance_to_boss(client, boss_blind_key, max_tries=50):
    gs = ""
    for _ in range(max_tries):
        state = client.call("gamestate")
        gs = state.get("state", "")
        if gs == "SELECTING_HAND":
            blind_name, _ = get_current_blind(state)
            if blind_name not in ("Small Blind", "Big Blind"):
                return state
            _beat_blind_fast(client, state)
        elif gs == "ROUND_EVAL":
            client.call("cash_out")
            time.sleep(0.3)
        elif gs == "SHOP":
            client.call("next_round")
            time.sleep(0.3)
        elif gs == "BLIND_SELECT":
            if _is_boss_blind_select(state):
                try:
                    client.call("set", {"blind": boss_blind_key})
                except APIError as e:
                    print(f"  set blind failed: {e.message}")
            client.call("select")
            time.sleep(0.3)
        elif gs in ("HAND_PLAYED", "DRAW_TO_HAND", "NEW_ROUND"):
            time.sleep(0.3)
        else:
            time.sleep(0.3)
    raise TimeoutError(f"Never reached boss blind, stuck in {gs}")


def setup_round(client, seed, joker_keys=None, card_configs=None,
                boss_blind_key=None, ante=None):
    """Start a game, optionally advance to a boss blind, inject jokers + cards.

    After injection, calls set({debuff: true}) to re-apply blind debuffs
    to all hand cards (needed because add() bypasses Blind:debuff_card()).
    """
    try:
        client.call("menu")
    except APIError:
        pass
    time.sleep(0.5)
    client.call("start", {"deck": "RED", "stake": "WHITE", "seed": seed})
    state = wait_for_state(client, ["SELECTING_HAND"])

    if ante:
        try:
            client.call("set", {"ante": ante})
        except APIError as e:
            print(f"  set ante failed: {e.message}")

    if boss_blind_key:
        _beat_blind_fast(client, state)
        state = _advance_to_boss(client, boss_blind_key)
        blind_name, _ = get_current_blind(state)
        print(f"  At boss blind: {blind_name}")

    # Clear existing jokers
    for i in range(state.get("jokers", {}).get("count", 0)):
        try:
            client.call("sell", {"joker": 0})
        except APIError:
            pass

    # Discard existing hand to make room for injected cards
    for _ in range(3):
        state = client.call("gamestate")
        hc = state.get("hand", {}).get("cards", [])
        if hc:
            try:
                client.call("discard", {"cards": list(range(min(len(hc), 5)))})
                time.sleep(0.2)
            except APIError:
                break

    # Add jokers
    if joker_keys:
        for jk in joker_keys:
            params = {"key": jk} if isinstance(jk, str) else jk
            try:
                client.call("add", params)
            except APIError as e:
                print(f"  FAILED joker {params}: {e.message}")

    # Add controlled hand cards
    if card_configs:
        for cfg in card_configs:
            params = {"key": cfg["key"]}
            for k in ("edition", "enhancement", "seal"):
                if k in cfg:
                    params[k] = cfg[k]
            try:
                client.call("add", params)
            except APIError as e:
                print(f"  FAILED card {cfg['key']}: {e.message}")

    # Re-apply blind debuffs to injected cards
    if boss_blind_key:
        try:
            client.call("set", {"debuff": True})
        except APIError as e:
            print(f"  set debuff failed: {e.message}")

    time.sleep(0.3)
    return client.call("gamestate")


def play_and_compare(client, state, play_count=5, label="", blind_name_override=None):
    """Play the last N cards and compare bot estimate vs game actual."""
    jokers = state.get("jokers", {}).get("cards", [])
    hand_cards = state.get("hand", {}).get("cards", [])
    hand_levels = state.get("hands", {})

    blind_name = blind_name_override
    if not blind_name:
        blind_name, _ = get_current_blind(state)

    if blind_name == "The Flint":
        hand_levels = flint_halve_hand_levels(hand_levels)

    play_indices = list(range(len(hand_cards) - play_count, len(hand_cards)))
    played = [hand_cards[i] for i in play_indices]
    held = [c for j, c in enumerate(hand_cards) if j not in set(play_indices)]

    joker_key_set = {j.get("key") for j in jokers}
    hand_name = classify_hand(played)
    scoring = played if "j_splash" in joker_key_set else _scoring_cards_for(hand_name, played)

    detail = score_hand_detailed(
        hand_name, scoring,
        hand_levels=hand_levels,
        jokers=jokers,
        played_cards=played,
        held_cards=held,
        money=state.get("money", 0),
        discards_left=state.get("round", {}).get("discards_left", 0),
        hands_left=state.get("round", {}).get("hands_left", 4),
        joker_limit=state.get("jokers", {}).get("limit", 5),
        blind_name=blind_name,
    )

    pre_chips = state.get("round", {}).get("chips", 0)
    print(f"  Blind: {blind_name}")
    print(f"  Hand: {hand_name}")
    print(f"  Playing: {[c.get('label','?') for c in played]}")
    debuffed_labels = [c.get('label','?') for c in played if is_debuffed(c)]
    if debuffed_labels:
        print(f"  Debuffed: {debuffed_labels}")
    print(f"  Held: {[c.get('label','?') for c in held]}")
    print(f"  Jokers: {[j.get('key','?') for j in jokers]}")
    print(f"  Base: {detail['base_chips']}/{detail['base_mult']}")
    print(f"  Pre-joker: {detail['pre_joker_chips']}/{detail['pre_joker_mult']:.2f}")
    for entry in detail.get("joker_contributions", []):
        jlabel, dc, dm = entry[0], entry[1], entry[2]
        xm = entry[3] if len(entry) > 3 else 1.0
        parts = []
        if dc: parts.append(f"+{dc:.0f}c")
        if xm > 1.01 or xm < 0.99: parts.append(f"x{xm:.2f}")
        elif dm: parts.append(f"+{dm:.1f}m")
        if parts: print(f"    {jlabel}: {', '.join(parts)}")
    print(f"  Post-joker: {detail['post_joker_chips']}/{detail['post_joker_mult']:.2f}")
    print(f"  Est: {detail['total']}")

    try:
        new_state = client.call("play", {"cards": play_indices})
    except APIError as e:
        print(f"  Play failed: {e.message}")
        return None

    post_chips = new_state.get("round", {}).get("chips", 0)
    actual = post_chips - pre_chips
    diff = actual - detail["total"]

    status = "MATCH" if diff == 0 else f"MISMATCH({diff:+d})"
    print(f"  Actual: {actual}  Diff: {diff:+d}  {status}")

    return {"label": label, "est": detail["total"], "actual": actual, "diff": diff,
            "blind": blind_name, "hand": hand_name}


# =========================================================================
# THE WINDOW TESTS — debuffed cards + joker interactions
#
# The Window debuffs all Diamond cards. We inject controlled cards then
# call set({debuff: true}) to trigger Blind:debuff_card() on them.
# =========================================================================

def run_window_tests(client):
    """Test The Window boss debuffing cards and joker interaction with debuffed cards."""
    print("\n" + "=" * 60)
    print("THE WINDOW TESTS — debuffed card + joker interactions")
    print("=" * 60)
    results = []

    # -----------------------------------------------------------------
    # Test 1: Window + Ride the Bus — debuffed face card (D_Q)
    # Ride the Bus should fire because debuffed face cards shouldn't
    # count as face cards for the reset check.
    # Expected: 5 chips × (1 + 5 + 1) mult = 35  (base HC + RtB stored 5 + extra 1)
    # Bug pred: 5 chips × 1 mult = 5  (RtB resets because Q is "face")
    # -----------------------------------------------------------------
    print(f"\n--- Window + RtB + debuffed Qd (should fire) ---")
    state = setup_round(client, "WP1", boss_blind_key="bl_window", ante=2,
        joker_keys=["j_ride_the_bus"],
        card_configs=[
            {"key": "D_Q"},  # debuffed by Window (Diamond)
            {"key": "S_4"}, {"key": "S_5"}, {"key": "S_6"}, {"key": "S_7"},
        ])
    if state:
        r = play_and_compare(client, state, label="Window + RtB + debuffed Qd (should fire)")
        if r: results.append(r)

    # -----------------------------------------------------------------
    # Test 2: Window + Ride the Bus — debuffed King
    # Same as test 1 but with Kd.
    # -----------------------------------------------------------------
    print(f"\n--- Window + RtB + debuffed Kd ---")
    state = setup_round(client, "WP2", boss_blind_key="bl_window", ante=2,
        joker_keys=["j_ride_the_bus"],
        card_configs=[
            {"key": "D_K"},  # debuffed by Window
            {"key": "S_4"}, {"key": "S_5"}, {"key": "S_6"}, {"key": "S_7"},
        ])
    if state:
        r = play_and_compare(client, state, label="Window + RtB + debuffed Kd")
        if r: results.append(r)

    # -----------------------------------------------------------------
    # Test 3: Window + Ride the Bus — non-debuffed face (control)
    # Live Qs should trigger RtB reset → mult = 0 from RtB.
    # -----------------------------------------------------------------
    print(f"\n--- Window + RtB + live Qs (resets, control) ---")
    state = setup_round(client, "WP3", boss_blind_key="bl_window", ante=2,
        joker_keys=["j_ride_the_bus"],
        card_configs=[
            {"key": "S_Q"},  # NOT debuffed (Spade)
            {"key": "S_4"}, {"key": "S_5"}, {"key": "S_6"}, {"key": "S_7"},
        ])
    if state:
        r = play_and_compare(client, state, label="Window + RtB + live Qs (resets)")
        if r: results.append(r)

    # -----------------------------------------------------------------
    # Test 4: Window + Ride the Bus — no face cards (control)
    # All non-face, non-diamond. RtB fires unconditionally.
    # -----------------------------------------------------------------
    print(f"\n--- Window + RtB + no face (control) ---")
    state = setup_round(client, "WP4", boss_blind_key="bl_window", ante=2,
        joker_keys=["j_ride_the_bus"],
        card_configs=[
            {"key": "S_2"}, {"key": "S_3"}, {"key": "S_4"},
            {"key": "S_5"}, {"key": "S_6"},
        ])
    if state:
        r = play_and_compare(client, state, label="Window + RtB + no face (control)")
        if r: results.append(r)

    # -----------------------------------------------------------------
    # Test 5: Window + Smiley Face — debuffed face card
    # Smiley gives +5 per face card scored. Debuffed face shouldn't count.
    # -----------------------------------------------------------------
    print(f"\n--- Window + Smiley + debuffed Jd ---")
    state = setup_round(client, "WP5", boss_blind_key="bl_window", ante=2,
        joker_keys=["j_smiley"],
        card_configs=[
            {"key": "D_J"},  # debuffed by Window
            {"key": "S_4"}, {"key": "S_5"}, {"key": "S_6"}, {"key": "S_7"},
        ])
    if state:
        r = play_and_compare(client, state, label="Window + Smiley + debuffed Jd (no +mult)")
        if r: results.append(r)

    # -----------------------------------------------------------------
    # Test 6: Window + Photograph — debuffed first face card
    # Photograph x2 shouldn't fire if the only face card is debuffed.
    # -----------------------------------------------------------------
    print(f"\n--- Window + Photograph + debuffed Kd ---")
    state = setup_round(client, "WP6", boss_blind_key="bl_window", ante=2,
        joker_keys=["j_photograph"],
        card_configs=[
            {"key": "D_K"},  # debuffed by Window
            {"key": "S_4"}, {"key": "S_5"}, {"key": "S_6"}, {"key": "S_7"},
        ])
    if state:
        r = play_and_compare(client, state, label="Window + Photograph + debuffed Kd (no x2)")
        if r: results.append(r)

    # -----------------------------------------------------------------
    # Test 7: Window + Green Joker — debuffed cards baseline
    # Green Joker fires unconditionally. Confirms joker phase runs
    # even with debuffed scoring cards.
    # -----------------------------------------------------------------
    print(f"\n--- Window + Green Joker (unconditional) ---")
    state = setup_round(client, "WP7", boss_blind_key="bl_window", ante=2,
        joker_keys=["j_green_joker"],
        card_configs=[
            {"key": "D_Q"},  # debuffed by Window
            {"key": "S_4"}, {"key": "S_5"}, {"key": "S_6"}, {"key": "S_7"},
        ])
    if state:
        r = play_and_compare(client, state, label="Window + Green Joker + debuffed Qd")
        if r: results.append(r)

    # -----------------------------------------------------------------
    # Test 8: Window + Baron — debuffed held King
    # D_K held and debuffed → shouldn't trigger Baron x1.5.
    # -----------------------------------------------------------------
    print(f"\n--- Window + Baron + debuffed held Kd ---")
    state = setup_round(client, "WP8", boss_blind_key="bl_window", ante=2,
        joker_keys=["j_baron"],
        card_configs=[
            # Play 5, hold debuffed Kd
            {"key": "S_A"}, {"key": "S_2"}, {"key": "S_3"},
            {"key": "S_4"}, {"key": "S_5"}, {"key": "D_K"},
        ])
    if state:
        r = play_and_compare(client, state, label="Window + Baron + debuffed held Kd")
        if r: results.append(r)

    return results


# =========================================================================
# PAREIDOLIA TESTS — all cards are face cards
#
# Pareidolia doesn't involve debuffs, so injected cards work fine.
# =========================================================================

def run_pareidolia_tests(client):
    """Test Pareidolia making all cards count as face cards for joker interactions."""
    print("\n" + "=" * 60)
    print("PAREIDOLIA TESTS — all cards are face cards")
    print("=" * 60)
    results = []

    # -----------------------------------------------------------------
    # Test 1: Pareidolia + Smiley Face (5 number cards)
    # Without Pareidolia, Smiley gives +0 for numbers.
    # With Pareidolia, all 5 are face → +25 mult.
    # -----------------------------------------------------------------
    print(f"\n--- Pareidolia + Smiley Face (5 number cards) ---")
    state = setup_round(client, "PA1",
        joker_keys=["j_pareidolia", "j_smiley"],
        card_configs=[
            {"key": "S_4"}, {"key": "H_5"}, {"key": "D_6"},
            {"key": "C_7"}, {"key": "S_8"},
        ])
    if state:
        r = play_and_compare(client, state, label="Pareidolia + Smiley (5 numbers)")
        if r: results.append(r)

    # -----------------------------------------------------------------
    # Test 2: Pareidolia + Photograph (first face = x2)
    # With Pareidolia, the first card scored is always a "face card".
    # -----------------------------------------------------------------
    print(f"\n--- Pareidolia + Photograph (number cards) ---")
    state = setup_round(client, "PA2",
        joker_keys=["j_pareidolia", "j_photograph"],
        card_configs=[
            {"key": "S_2"}, {"key": "H_3"}, {"key": "D_4"},
            {"key": "C_5"}, {"key": "S_6"},
        ])
    if state:
        r = play_and_compare(client, state, label="Pareidolia + Photograph (numbers)")
        if r: results.append(r)

    # -----------------------------------------------------------------
    # Test 3: Pareidolia + Smiley + Photograph combined
    # -----------------------------------------------------------------
    print(f"\n--- Pareidolia + Smiley + Photograph ---")
    state = setup_round(client, "PA3",
        joker_keys=["j_pareidolia", "j_smiley", "j_photograph"],
        card_configs=[
            {"key": "S_3"}, {"key": "H_4"}, {"key": "D_5"},
            {"key": "C_6"}, {"key": "S_7"},
        ])
    if state:
        r = play_and_compare(client, state, label="Pareidolia + Smiley + Photo (Straight)")
        if r: results.append(r)

    # -----------------------------------------------------------------
    # Test 4: Pareidolia + Baron (held non-Kings)
    # Baron gives x1.5 per held King. Pareidolia makes all cards "face"
    # but Baron checks rank == K. Non-K held cards should NOT trigger Baron.
    # -----------------------------------------------------------------
    print(f"\n--- Pareidolia + Baron (non-King held cards) ---")
    state = setup_round(client, "PA4",
        joker_keys=["j_pareidolia", "j_baron"],
        card_configs=[
            # Play 3 Aces, hold 2 non-Kings
            {"key": "S_A"}, {"key": "H_A"}, {"key": "D_A"},
            {"key": "C_5"}, {"key": "S_6"},
        ])
    if state:
        r = play_and_compare(client, state, play_count=3, label="Pareidolia + Baron (no held Kings)")
        if r: results.append(r)

    # -----------------------------------------------------------------
    # Test 5: Pareidolia + Baron (held Kings present)
    # Confirm Baron fires for actual Kings, regardless of Pareidolia.
    # -----------------------------------------------------------------
    print(f"\n--- Pareidolia + Baron (held Kings) ---")
    state = setup_round(client, "PA5",
        joker_keys=["j_pareidolia", "j_baron"],
        card_configs=[
            # Play 3 Aces, hold 2 Kings
            {"key": "S_A"}, {"key": "H_A"}, {"key": "D_A"},
            {"key": "C_K"}, {"key": "S_K"},
        ])
    if state:
        r = play_and_compare(client, state, play_count=3, label="Pareidolia + Baron (2 held Kings)")
        if r: results.append(r)

    # -----------------------------------------------------------------
    # Test 6: Pareidolia + Ride the Bus (always resets)
    # Pareidolia makes all cards face → Ride the Bus always resets.
    # -----------------------------------------------------------------
    print(f"\n--- Pareidolia + Ride the Bus (always resets) ---")
    state = setup_round(client, "PA6",
        joker_keys=["j_pareidolia", "j_ride_the_bus"],
        card_configs=[
            {"key": "S_2"}, {"key": "H_3"}, {"key": "D_4"},
            {"key": "C_5"}, {"key": "S_6"},
        ])
    if state:
        r = play_and_compare(client, state, label="Pareidolia + RtB (always resets)")
        if r: results.append(r)

    # -----------------------------------------------------------------
    # Test 7: Pareidolia + Scary Face
    # Scary Face gives +30 chips per face card scored.
    # With Pareidolia, all 5 cards → +150 chips.
    # -----------------------------------------------------------------
    print(f"\n--- Pareidolia + Scary Face ---")
    state = setup_round(client, "PA7",
        joker_keys=["j_pareidolia", "j_scary_face"],
        card_configs=[
            {"key": "S_2"}, {"key": "H_3"}, {"key": "D_4"},
            {"key": "C_5"}, {"key": "S_6"},
        ])
    if state:
        r = play_and_compare(client, state, label="Pareidolia + Scary Face (5 numbers)")
        if r: results.append(r)

    # -----------------------------------------------------------------
    # Test 8: Full batch 073 repro — Pareidolia + Smiley + Photo + Baron
    # Play 5 number cards as Straight, hold 3.
    # -----------------------------------------------------------------
    print(f"\n--- Pareidolia + Smiley + Photo + Baron (batch073 repro) ---")
    state = setup_round(client, "PA8",
        joker_keys=["j_pareidolia", "j_smiley", "j_photograph", "j_baron"],
        card_configs=[
            {"key": "S_3"}, {"key": "H_4"}, {"key": "D_5"},
            {"key": "C_6"}, {"key": "S_7"},
            {"key": "H_9"}, {"key": "D_T"}, {"key": "C_2"},
        ])
    if state:
        r = play_and_compare(client, state, play_count=5, label="Pareidolia + Smiley + Photo + Baron (Straight)")
        if r: results.append(r)

    # -----------------------------------------------------------------
    # Test 9: Smiley alone (no Pareidolia, control)
    # Smiley gives +0 for number cards without Pareidolia.
    # -----------------------------------------------------------------
    print(f"\n--- Smiley Face alone (no Pareidolia, control) ---")
    state = setup_round(client, "PA9",
        joker_keys=["j_smiley"],
        card_configs=[
            {"key": "S_4"}, {"key": "H_5"}, {"key": "D_6"},
            {"key": "C_7"}, {"key": "S_8"},
        ])
    if state:
        r = play_and_compare(client, state, label="Smiley alone (no Pareidolia, control)")
        if r: results.append(r)

    # -----------------------------------------------------------------
    # Test 10: Pareidolia + Smiley + Photo with actual face cards
    # Redundant Pareidolia — confirms no double-counting.
    # -----------------------------------------------------------------
    print(f"\n--- Pareidolia + Smiley + Photo with actual face cards ---")
    state = setup_round(client, "PA10",
        joker_keys=["j_pareidolia", "j_smiley", "j_photograph"],
        card_configs=[
            {"key": "S_J"}, {"key": "H_Q"}, {"key": "D_K"},
            {"key": "C_J"}, {"key": "S_Q"},
        ])
    if state:
        r = play_and_compare(client, state, label="Pareidolia + Smiley + Photo (actual face)")
        if r: results.append(r)

    return results


# =========================================================================
# Main
# =========================================================================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=None,
                        help="Server port (default: auto with --start-server, 12346 without)")
    parser.add_argument("--start-server", action="store_true",
                        help="Auto-start a balatrobot server (killed on exit)")
    parser.add_argument("--skip-window", action="store_true")
    parser.add_argument("--skip-pareidolia", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    server_proc = None

    if args.start_server:
        from harness import TEST_PORT, ensure_server, stop_server as _stop
        port = args.port or TEST_PORT
        client, server_proc = ensure_server(port)
    else:
        _stop = lambda p: None
        port = args.port or 12346
        client = BalatroClient(port=port)
        try:
            client.call("health")
        except Exception as e:
            print(f"No server on port {port}: {e}")
            sys.exit(1)

    all_results = []

    try:
        if not args.skip_window:
            all_results.extend(run_window_tests(client))
        else:
            print("\n  [Skipping Window tests]")

        if not args.skip_pareidolia:
            all_results.extend(run_pareidolia_tests(client))
        else:
            print("\n  [Skipping Pareidolia tests]")

        # ===================================================================
        print(f"\n\n{'=' * 60}")
        print("SUMMARY")
        print(f"{'=' * 60}")
        matches = 0
        mismatches = 0
        for r in all_results:
            if r is None:
                continue
            status = "MATCH" if r["diff"] == 0 else f"MISMATCH({r['diff']:+d})"
            if r["diff"] == 0:
                matches += 1
            else:
                mismatches += 1
            print(f"  {r['label']:60s} est={r['est']:>6d} actual={r['actual']:>6d} {status}")

        total = matches + mismatches
        print(f"\n  Total: {matches} match, {mismatches} mismatch out of {total}")

    finally:
        if server_proc:
            _stop(server_proc)

    if mismatches > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
