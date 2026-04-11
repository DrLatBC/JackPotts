"""Test Glass + Polychrome xMult stacking.

Injects a hand of Glass+Polychrome cards and verifies that the bot's
scoring estimate matches the actual game score (both should apply:
Glass x2.0 * Polychrome x1.5 = x3.0 per card).

This validates the mod's API fix (separating edition_x_mult from
enhancement_x_mult in gamestate.lua) and the bot's card_xmult_value().

Usage:
    python test_glass_poly.py [--port PORT]

Requires a running balatrobot server.
"""

import argparse
import logging
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "support"))

from balatrobot.cli.client import BalatroClient, APIError
from balatro_bot.domain.scoring.classify import classify_hand, _scoring_cards_for
from balatro_bot.domain.scoring.estimate import score_hand_detailed
from balatro_bot.cards import card_xmult_value, _modifier
from harness import wait_for_state


def main():
    parser = argparse.ArgumentParser(description="Test Glass+Polychrome xMult stacking")
    parser.add_argument("--port", type=int, default=12346)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    # Set up scoring log to console
    scoring_log = logging.getLogger("balatro_scoring")
    scoring_log.setLevel(logging.INFO)
    if not scoring_log.handlers:
        sh = logging.StreamHandler()
        sh.setFormatter(logging.Formatter("%(asctime)s [SCORE] %(message)s", datefmt="%H:%M:%S"))
        scoring_log.addHandler(sh)

    client = BalatroClient(port=args.port)

    try:
        client.call("health")
    except Exception as e:
        print(f"No server on port {args.port}: {e}")
        sys.exit(1)

    # Start a fresh game
    print("Starting new game...")
    try:
        client.call("menu")
    except APIError:
        pass
    time.sleep(0.5)

    state = client.call("start", {"deck": "RED", "stake": "WHITE", "seed": "GLASSPOLY"})
    print(f"Game started: seed={state.get('seed')} state={state.get('state')}")

    # Get to SELECTING_HAND
    state = wait_for_state(client, ["SELECTING_HAND"])
    print(f"In state: {state.get('state')}")

    # Sell all existing jokers to have a clean slate (no joker interference)
    joker_count = state.get("jokers", {}).get("count", 0)
    for i in range(joker_count):
        try:
            client.call("sell", {"joker": 0})
        except APIError:
            pass

    # Discard entire hand to clear it
    hand_cards = state.get("hand", {}).get("cards", [])
    if hand_cards:
        indices = list(range(min(len(hand_cards), 5)))
        try:
            client.call("discard", {"cards": indices})
            time.sleep(0.2)
        except APIError:
            pass

    # Now inject Glass+Polychrome cards into the hand
    # Add 5 Kings with Glass enhancement + Polychrome edition
    print("\nInjecting Glass+Polychrome cards...")
    test_cards = ["S_K", "H_K", "D_K", "C_K", "S_Q"]
    for card_key in test_cards:
        try:
            client.call("add", {
                "key": card_key,
                "edition": "POLYCHROME",
                "enhancement": "GLASS",
            })
            print(f"  Added {card_key} (Glass+Polychrome)")
        except APIError as e:
            print(f"  Failed to add {card_key}: {e.message}")

    # Re-read state to see the injected cards
    time.sleep(0.3)
    state = client.call("gamestate")
    hand_cards = state.get("hand", {}).get("cards", [])

    print(f"\nHand has {len(hand_cards)} cards")

    # Find Glass+Poly cards and verify API fields
    glass_poly_cards = []
    for i, c in enumerate(hand_cards):
        mod = _modifier(c)
        enh = mod.get("enhancement", "")
        ed = mod.get("edition", "")
        ed_xm = mod.get("edition_x_mult")
        enh_xm = mod.get("enhancement_x_mult")
        rank = c.get("value", {}).get("rank", "?")
        suit = c.get("value", {}).get("suit", "?")

        if enh == "GLASS" and ed == "POLYCHROME":
            glass_poly_cards.append(i)
            xmult = card_xmult_value(c)
            print(f"  [{i}] {rank}{suit} Glass+Poly | edition_x_mult={ed_xm} enhancement_x_mult={enh_xm} | bot_xmult={xmult}")

            # Verify the API now separates them
            if enh_xm is not None:
                print(f"       enhancement_x_mult field present: {enh_xm} (expected ~2.0)")
            else:
                print(f"       WARNING: enhancement_x_mult field MISSING (mod fix not applied?)")

            if ed_xm is not None and abs(ed_xm - 1.5) < 0.01:
                print(f"       edition_x_mult correct: {ed_xm} (Polychrome 1.5)")
            elif ed_xm is not None:
                print(f"       WARNING: edition_x_mult={ed_xm} (expected 1.5, got contaminated value?)")
            else:
                print(f"       edition_x_mult not set (will use fallback 1.5)")

            if abs(xmult - 3.0) < 0.01:
                print(f"       bot xmult CORRECT: {xmult} = Glass(2.0) * Poly(1.5)")
            else:
                print(f"       WARNING: bot xmult={xmult}, expected 3.0")

    if not glass_poly_cards:
        print("\nERROR: No Glass+Polychrome cards found in hand!")
        print("Raw hand data:")
        for i, c in enumerate(hand_cards):
            print(f"  [{i}] {c.get('value', {})} mod={_modifier(c)}")
        sys.exit(1)

    # Now play the cards and check scoring
    print(f"\nPlaying cards at indices {glass_poly_cards[:5]}...")
    play_indices = glass_poly_cards[:5]

    # Capture pre-play chips
    pre_chips = state.get("round", {}).get("chips", 0)
    print(f"  Pre-play chips: {pre_chips}")

    # Build the played cards list for scoring estimate
    played = [hand_cards[i] for i in play_indices]
    hand_name = classify_hand(played)
    jokers = state.get("jokers", {}).get("cards", [])
    hand_levels = state.get("hands", {})
    held = [c for j, c in enumerate(hand_cards) if j not in set(play_indices)]

    detail = score_hand_detailed(
        hand_name,
        _scoring_cards_for(hand_name, played),
        hand_levels=hand_levels,
        jokers=jokers,
        played_cards=played,
        held_cards=held,
        money=state.get("money", 0),
        discards_left=state.get("round", {}).get("discards_left", 0),
        hands_left=state.get("round", {}).get("hands_left", 1),
        joker_limit=state.get("jokers", {}).get("limit", 5),
    )

    print(f"\n  Hand type: {hand_name}")
    print(f"  Bot estimate: {detail['total']}")
    print(f"  Breakdown: base={detail['base_chips']}/{detail['base_mult']} "
          f"pre_joker={detail['pre_joker_chips']}/{detail['pre_joker_mult']:.1f} "
          f"post_joker={detail['post_joker_chips']}/{detail['post_joker_mult']:.1f}")

    # Play the hand
    try:
        new_state = client.call("play", {"cards": play_indices})
    except APIError as e:
        print(f"\n  Play failed: {e.message}")
        sys.exit(1)

    # Check actual score
    post_chips = new_state.get("round", {}).get("chips", 0)
    actual = post_chips - pre_chips
    diff = actual - detail["total"]

    print(f"  Actual score: {actual}")
    print(f"  Difference: {diff:+d}")

    print()
    print("=" * 60)
    if actual == 0:
        print("TEST INCONCLUSIVE: actual=0 (state may have advanced past scoring)")
    elif diff == 0:
        print("TEST PASSED: Bot estimate matches actual score exactly!")
        print(f"  Glass+Poly stacking confirmed: x3.0 applied correctly")
    elif abs(diff) < actual * 0.01:
        print(f"TEST PASSED (rounding): diff={diff:+d} ({abs(diff)/actual*100:.1f}%)")
    else:
        pct = abs(diff) / actual * 100 if actual else 0
        print(f"TEST FAILED: Mismatch of {diff:+d} ({pct:.1f}%)")
        if diff < 0 and abs(detail["total"] / actual - 1.5) < 0.05:
            print("  Pattern: estimate/actual ~ 1.5x — Polychrome x1.5 applied in bot but not game")
            print("  Likely cause: mod API still reporting contaminated edition_x_mult")
        elif diff > 0:
            print("  Bot under-estimated — game applied more than expected")
    print("=" * 60)


if __name__ == "__main__":
    main()
