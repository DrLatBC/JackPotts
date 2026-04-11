"""Integration tests for high-lift mismatch jokers.

Targets: Green Joker (100%), Obelisk (100%), Mystic Summit (42%),
Flower Pot (44%), Swashbuckler (34%), Constellation (51%), Ramen (57%).

Each test dumps ability dict + effect text + parsed values so we can
pinpoint exactly where the model diverges from the game.

Usage:
    python test_mismatch_lift.py [--port PORT]
"""

import argparse
import logging
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "support"))

from balatrobot.cli.client import BalatroClient, APIError
from harness import wait_for_state, setup_game_full as setup_game
from balatro_bot.domain.scoring.classify import classify_hand, _scoring_cards_for
from balatro_bot.domain.scoring.estimate import score_hand_detailed
from balatro_bot.cards import card_chip_value, _modifier
from balatro_bot.joker_effects.parsers import parse_effect_value, _ability, _ab_mult, _ab_xmult


def dump_jokers(jokers):
    for j in jokers:
        key = j.get("key", "?")
        ab = j.get("value", {}).get("ability", {})
        effect = j.get("value", {}).get("effect", "")
        parsed = parse_effect_value(effect)
        ab_mult = _ab_mult(j, fallback=-999)
        ab_xmult = _ab_xmult(j, fallback=-999)
        print(f"    {key:20s}")
        print(f"      ability  = {ab}")
        print(f"      effect   = {effect[:140]}")
        print(f"      parsed   = {parsed}")
        print(f"      _ab_mult = {ab_mult}, _ab_xmult = {ab_xmult}")


def score_and_play(client, state, hand_cards, play_indices):
    """Score with bot model, play in game, compare. Returns result dict."""
    jokers = state.get("jokers", {}).get("cards", [])
    hl = state.get("round", {}).get("hands_left", 4)

    played = [hand_cards[i] for i in play_indices]
    held = [c for j, c in enumerate(hand_cards) if j not in set(play_indices)]

    hand_name = classify_hand(played)
    joker_key_set = {j.get("key") for j in jokers}
    scoring = played if "j_splash" in joker_key_set else _scoring_cards_for(hand_name, played)

    detail = score_hand_detailed(
        hand_name, scoring,
        hand_levels=state.get("hands", {}),
        jokers=jokers,
        played_cards=played,
        held_cards=held,
        money=state.get("money", 0),
        discards_left=state.get("round", {}).get("discards_left", 0),
        hands_left=hl,
        joker_limit=state.get("jokers", {}).get("limit", 5),
    )

    pre_chips = state.get("round", {}).get("chips", 0)
    print(f"  Hand: {hand_name}")
    print(f"  Playing: {[c.get('label','?') for c in played]}")
    print(f"  Holding: {[c.get('label','?') for c in held]}")
    print(f"  Base: {detail['base_chips']}/{detail['base_mult']}")
    print(f"  Pre-joker: {detail['pre_joker_chips']}/{detail['pre_joker_mult']:.1f}")
    for entry in detail.get("joker_contributions", []):
        jlabel, dc, dm = entry[0], entry[1], entry[2]
        xm = entry[3] if len(entry) > 3 else 1.0
        parts = []
        if dc: parts.append(f"+{dc:.0f}c")
        if xm > 1.01 or xm < 0.99: parts.append(f"x{xm:.2f}")
        elif dm: parts.append(f"+{dm:.1f}m")
        if parts: print(f"    {jlabel}: {', '.join(parts)}")
    print(f"  Post-joker: {detail['post_joker_chips']}/{detail['post_joker_mult']:.1f}")
    print(f"  Bot estimate: {detail['total']}")

    try:
        new_state = client.call("play", {"cards": play_indices})
    except APIError as e:
        print(f"  Play failed: {e.message}")
        return None

    post_chips = new_state.get("round", {}).get("chips", 0)
    actual = post_chips - pre_chips
    diff = actual - detail["total"]

    print(f"  Actual score: {actual}")
    print(f"  Diff: {diff:+d}  {'MATCH' if diff == 0 else 'MISMATCH'}")

    return {"est": detail["total"], "actual": actual, "diff": diff}


def run_simple_test(client, label, seed, joker_keys, hand, play_count=None):
    if play_count is None:
        play_count = len(hand)

    print(f"\n{'='*60}")
    print(f"TEST: {label}")
    print(f"{'='*60}")

    state = setup_game(client, seed, joker_keys=joker_keys, card_configs=hand)
    jokers = state.get("jokers", {}).get("cards", [])
    hand_cards = state.get("hand", {}).get("cards", [])

    print(f"  hands_left={state.get('round',{}).get('hands_left','?')}")
    print(f"  discards_left={state.get('round',{}).get('discards_left','?')}")
    dump_jokers(jokers)

    play_indices = list(range(len(hand_cards) - play_count, len(hand_cards)))
    result = score_and_play(client, state, hand_cards, play_indices)
    if result:
        result["label"] = label
    return result


# =====================================================================
# GREEN JOKER — suspected pre-increment issue (+1 mult per hand played)
# =====================================================================

def test_green_joker_fresh(client):
    """Fresh Green Joker, first hand. Ability should show mult=0, game adds +1 before scoring."""
    return run_simple_test(client,
        "Green Joker: fresh (first hand, expect +1 pre-increment)",
        "GJ1", ["j_green_joker"],
        [{"key": "S_K"}, {"key": "H_K"}, {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"}])


def test_green_joker_second_hand(client):
    """Play two hands with Green Joker to see accumulation."""
    print(f"\n{'='*60}")
    print("TEST: Green Joker: two-hand accumulation")
    print(f"{'='*60}")

    hand1 = [{"key": "S_K"}, {"key": "H_K"}, {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"}]
    state = setup_game(client, "GJ2", joker_keys=["j_green_joker"], card_configs=hand1)
    hand_cards = state.get("hand", {}).get("cards", [])

    print("  --- Play 1 ---")
    dump_jokers(state.get("jokers", {}).get("cards", []))
    play_indices = list(range(len(hand_cards) - 5, len(hand_cards)))
    r1 = score_and_play(client, state, hand_cards, play_indices)

    time.sleep(0.5)
    state2 = wait_for_state(client, ["SELECTING_HAND"])

    # Add cards for play 2
    for cfg in [{"key": "S_A"}, {"key": "H_A"}, {"key": "D_6"}, {"key": "C_2"}, {"key": "S_7"}]:
        try:
            client.call("add", cfg)
        except APIError as e:
            print(f"  FAILED: {e.message}")
    time.sleep(0.3)
    state2 = client.call("gamestate")
    hand_cards2 = state2.get("hand", {}).get("cards", [])

    print("\n  --- Play 2 ---")
    dump_jokers(state2.get("jokers", {}).get("cards", []))
    play_indices2 = list(range(len(hand_cards2) - 5, len(hand_cards2)))
    r2 = score_and_play(client, state2, hand_cards2, play_indices2)

    results = []
    if r1:
        r1["label"] = "Green Joker: play 1"
        results.append(r1)
    if r2:
        r2["label"] = "Green Joker: play 2"
        results.append(r2)
    return results


# =====================================================================
# OBELISK — suspected pre-increment + conditional reset
# =====================================================================

def test_obelisk_fresh(client):
    """Fresh Obelisk, first hand. Should start at X1 and possibly increment."""
    return run_simple_test(client,
        "Obelisk: fresh (first hand)",
        "OB1", ["j_obelisk"],
        [{"key": "S_K"}, {"key": "H_K"}, {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"}])


def test_obelisk_two_hands(client):
    """Play two different hand types to see Obelisk accumulation vs reset."""
    print(f"\n{'='*60}")
    print("TEST: Obelisk: two hands (Pair then High Card)")
    print(f"{'='*60}")

    hand1 = [{"key": "S_K"}, {"key": "H_K"}, {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"}]
    state = setup_game(client, "OB2", joker_keys=["j_obelisk"], card_configs=hand1)
    hand_cards = state.get("hand", {}).get("cards", [])

    print("  --- Play 1: Pair ---")
    dump_jokers(state.get("jokers", {}).get("cards", []))
    play_indices = list(range(len(hand_cards) - 5, len(hand_cards)))
    r1 = score_and_play(client, state, hand_cards, play_indices)

    time.sleep(0.5)
    state2 = wait_for_state(client, ["SELECTING_HAND"])

    # Play 2: High Card (different hand type to see if Obelisk increments or resets)
    for cfg in [{"key": "S_A"}, {"key": "D_6"}, {"key": "C_2"}, {"key": "H_7"}, {"key": "D_9"}]:
        try:
            client.call("add", cfg)
        except APIError as e:
            print(f"  FAILED: {e.message}")
    time.sleep(0.3)
    state2 = client.call("gamestate")
    hand_cards2 = state2.get("hand", {}).get("cards", [])

    print("\n  --- Play 2: High Card ---")
    dump_jokers(state2.get("jokers", {}).get("cards", []))
    play_indices2 = list(range(len(hand_cards2) - 5, len(hand_cards2)))
    r2 = score_and_play(client, state2, hand_cards2, play_indices2)

    results = []
    if r1:
        r1["label"] = "Obelisk: play 1 (Pair)"
        results.append(r1)
    if r2:
        r2["label"] = "Obelisk: play 2 (High Card, different type)"
        results.append(r2)
    return results


# =====================================================================
# MYSTIC SUMMIT — conditional on 0 discards remaining
# =====================================================================

def test_mystic_summit_with_discards(client):
    """Mystic Summit with discards remaining — should NOT fire."""
    return run_simple_test(client,
        "Mystic Summit: with discards (should NOT fire)",
        "MS1", ["j_mystic_summit"],
        [{"key": "S_K"}, {"key": "H_K"}, {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"}])


def test_mystic_summit_no_discards(client):
    """Mystic Summit with 0 discards remaining — should fire +15 mult."""
    print(f"\n{'='*60}")
    print("TEST: Mystic Summit: 0 discards (should fire +15)")
    print(f"{'='*60}")

    # Use all discards first, then play
    try:
        client.call("menu")
    except APIError:
        pass
    time.sleep(0.5)
    client.call("start", {"deck": "RED", "stake": "WHITE", "seed": "MS2"})
    state = wait_for_state(client, ["SELECTING_HAND"])

    # Sell starting jokers
    for i in range(state.get("jokers", {}).get("count", 0)):
        try:
            client.call("sell", {"joker": 0})
        except APIError:
            pass

    # Add Mystic Summit
    try:
        client.call("add", {"key": "j_mystic_summit"})
    except APIError as e:
        print(f"  FAILED: {e.message}")

    # Burn all discards (RED deck has 3 + setup already used 2)
    for _ in range(5):
        state = client.call("gamestate")
        dl = state.get("round", {}).get("discards_left", 0)
        if dl <= 0:
            break
        hc = state.get("hand", {}).get("cards", [])
        if hc:
            try:
                client.call("discard", {"cards": [0]})
                time.sleep(0.2)
            except APIError:
                break

    # Add test hand
    for cfg in [{"key": "S_K"}, {"key": "H_K"}, {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"}]:
        try:
            client.call("add", cfg)
        except APIError as e:
            print(f"  FAILED: {e.message}")

    time.sleep(0.3)
    state = client.call("gamestate")
    jokers = state.get("jokers", {}).get("cards", [])
    hand_cards = state.get("hand", {}).get("cards", [])

    print(f"  discards_left={state.get('round',{}).get('discards_left','?')}")
    dump_jokers(jokers)

    play_indices = list(range(len(hand_cards) - 5, len(hand_cards)))
    result = score_and_play(client, state, hand_cards, play_indices)
    if result:
        result["label"] = "Mystic Summit: 0 discards (should fire)"
    return result


# =====================================================================
# FLOWER POT — needs all 4 suits in scoring cards
# =====================================================================

def test_flower_pot_all_suits(client):
    """Flower Pot with all 4 suits in a Flush Five... no. Use a hand with 4 suits scoring."""
    # Two Pair with 4 suits in the scoring cards
    return run_simple_test(client,
        "Flower Pot: 4 suits in scoring (Two Pair, should fire)",
        "FP1", ["j_flower_pot"],
        [{"key": "S_K"}, {"key": "H_K"}, {"key": "D_3"}, {"key": "C_3"}, {"key": "S_5"}])


def test_flower_pot_missing_suit(client):
    """Flower Pot with only 2 suits in scoring — should NOT fire."""
    return run_simple_test(client,
        "Flower Pot: 2 suits only (Pair, should NOT fire)",
        "FP2", ["j_flower_pot"],
        [{"key": "S_K"}, {"key": "H_K"}, {"key": "S_3"}, {"key": "S_4"}, {"key": "S_5"}])


def test_flower_pot_wild_card(client):
    """Flower Pot with Wild card filling a missing suit."""
    return run_simple_test(client,
        "Flower Pot: Wild fills missing suit",
        "FP3", ["j_flower_pot"],
        [{"key": "S_K"}, {"key": "H_K"}, {"key": "S_3", "enhancement": "WILD"}, {"key": "S_4"}, {"key": "S_5"}])


# =====================================================================
# SWASHBUCKLER — mult = sell value of all other jokers
# =====================================================================

def test_swashbuckler_alone(client):
    """Swashbuckler alone — no other jokers, should give +0 mult."""
    return run_simple_test(client,
        "Swashbuckler: alone (should be +0)",
        "SW1", ["j_swashbuckler"],
        [{"key": "S_K"}, {"key": "H_K"}, {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"}])


def test_swashbuckler_with_jokers(client):
    """Swashbuckler with 2 other jokers — check sell values add up."""
    return run_simple_test(client,
        "Swashbuckler: +2 jokers (check sell value sum)",
        "SW2", ["j_swashbuckler", "j_jolly", "j_zany"],
        [{"key": "S_K"}, {"key": "H_K"}, {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"}])


# =====================================================================
# CONSTELLATION — scaling xmult from planet use
# =====================================================================

def test_constellation_fresh(client):
    """Fresh Constellation, no planets used — should be X1.0 (noop)."""
    return run_simple_test(client,
        "Constellation: fresh (X1.0, noop)",
        "CON1", ["j_constellation"],
        [{"key": "S_K"}, {"key": "H_K"}, {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"}])


# =====================================================================
# RAMEN — decay xmult after discards
# =====================================================================

def test_ramen_fresh(client):
    """Fresh Ramen, no discards — should be X2.0."""
    return run_simple_test(client,
        "Ramen: fresh (X2.0)",
        "RAM1", ["j_ramen"],
        [{"key": "S_K"}, {"key": "H_K"}, {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"}])


def test_ramen_after_discards(client):
    """Ramen after discarding cards — check decay tracking."""
    print(f"\n{'='*60}")
    print("TEST: Ramen: after discards (decayed)")
    print(f"{'='*60}")

    try:
        client.call("menu")
    except APIError:
        pass
    time.sleep(0.5)
    client.call("start", {"deck": "RED", "stake": "WHITE", "seed": "RAM2"})
    state = wait_for_state(client, ["SELECTING_HAND"])

    for i in range(state.get("jokers", {}).get("count", 0)):
        try:
            client.call("sell", {"joker": 0})
        except APIError:
            pass

    try:
        client.call("add", {"key": "j_ramen"})
    except APIError as e:
        print(f"  FAILED: {e.message}")

    # Discard 5 cards twice = 10 cards discarded = -0.01 * 10 = -0.10
    # Ramen starts at X2.0, should be X1.90 after
    for _ in range(2):
        state = client.call("gamestate")
        hc = state.get("hand", {}).get("cards", [])
        if hc:
            try:
                client.call("discard", {"cards": list(range(min(len(hc), 5)))})
                time.sleep(0.3)
            except APIError:
                break

    # Add test hand
    for cfg in [{"key": "S_K"}, {"key": "H_K"}, {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"}]:
        try:
            client.call("add", cfg)
        except APIError as e:
            print(f"  FAILED: {e.message}")

    time.sleep(0.3)
    state = client.call("gamestate")
    jokers = state.get("jokers", {}).get("cards", [])
    hand_cards = state.get("hand", {}).get("cards", [])

    print(f"  discards_left={state.get('round',{}).get('discards_left','?')}")
    dump_jokers(jokers)

    play_indices = list(range(len(hand_cards) - 5, len(hand_cards)))
    result = score_and_play(client, state, hand_cards, play_indices)
    if result:
        result["label"] = "Ramen: after 10 discards (expect ~X1.90)"
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=12346)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    client = BalatroClient(port=args.port)
    try:
        client.call("health")
    except Exception as e:
        print(f"No server on port {args.port}: {e}")
        sys.exit(1)

    results = []

    # --- Green Joker ---
    results.append(test_green_joker_fresh(client))
    results.extend(test_green_joker_second_hand(client))

    # --- Obelisk ---
    results.append(test_obelisk_fresh(client))
    results.extend(test_obelisk_two_hands(client))

    # --- Mystic Summit ---
    results.append(test_mystic_summit_with_discards(client))
    r = test_mystic_summit_no_discards(client)
    if r:
        results.append(r)

    # --- Flower Pot ---
    results.append(test_flower_pot_all_suits(client))
    results.append(test_flower_pot_missing_suit(client))
    results.append(test_flower_pot_wild_card(client))

    # --- Swashbuckler ---
    results.append(test_swashbuckler_alone(client))
    results.append(test_swashbuckler_with_jokers(client))

    # --- Constellation ---
    results.append(test_constellation_fresh(client))

    # --- Ramen ---
    results.append(test_ramen_fresh(client))
    r = test_ramen_after_discards(client)
    if r:
        results.append(r)

    # ===================================================================
    # SUMMARY
    # ===================================================================
    print(f"\n\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    for r in results:
        if r is None:
            continue
        status = "MATCH" if r["diff"] == 0 else f"MISMATCH({r['diff']:+d})"
        print(f"  {r['label']:55s} est={r['est']:>8d} actual={r['actual']:>8d} {status}")

    mismatches = [r for r in results if r and r["diff"] != 0]
    if mismatches:
        print(f"\nFAILED: {len(mismatches)} mismatch(es)")
        sys.exit(1)
    print("\nPASSED: all scores matched")


if __name__ == "__main__":
    main()
