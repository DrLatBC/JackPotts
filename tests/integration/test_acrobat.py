"""Test Acrobat + Gros Michel interaction on the last hand.

Sets hands=1 directly via the set API so we're guaranteed to be on the final
hand without needing to burn hands (which can accidentally clear the blind).

Usage:
    python test_acrobat.py [--port PORT]
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
from balatro_bot.cards import card_chip_value, _modifier

from harness import wait_for_state, setup_game_full as setup_game


def run_test(client, label, seed, joker_keys, hands_left=None, hand=None):
    if hand is None:
        hand = [{"key": "S_K"}, {"key": "H_K"}, {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"}]

    print(f"\n{'='*60}")
    print(f"TEST: {label}")
    print(f"{'='*60}")

    state = setup_game(client, seed, joker_keys=joker_keys, card_configs=hand, hands_left=hands_left)
    jokers = state.get("jokers", {}).get("cards", [])
    hand_cards = state.get("hand", {}).get("cards", [])
    hl = state.get("round", {}).get("hands_left", "?")

    print(f"  hands_left={hl}")
    print(f"  Jokers:")
    for j in jokers:
        ab = j.get("value", {}).get("ability", {})
        effect = j.get("value", {}).get("effect", "")[:80]
        print(f"    {j.get('key'):20s} ability={ab} effect={effect}")

    play_indices = list(range(len(hand_cards) - len(hand), len(hand_cards)))
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

    return {"label": label, "est": detail["total"], "actual": actual, "diff": diff}


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

    # ---------------------------------------------------------------
    # 1. Acrobat on last hand (hands_left=1) — should apply x3
    # ---------------------------------------------------------------
    results.append(run_test(client, "Acrobat: last hand (hands=1)", "ACR1",
        ["j_acrobat"], hands_left=1))

    # ---------------------------------------------------------------
    # 2. Acrobat NOT last hand (hands_left=2) — should NOT apply x3
    # ---------------------------------------------------------------
    results.append(run_test(client, "Acrobat: not last hand (hands=2)", "ACR2",
        ["j_acrobat"], hands_left=2))

    # ---------------------------------------------------------------
    # 3. Acrobat + Gros Michel on last hand — x3 * +15 mult
    # ---------------------------------------------------------------
    results.append(run_test(client, "Acrobat + Gros Michel: last hand", "ACR3",
        ["j_gros_michel", "j_acrobat"], hands_left=1))

    # ---------------------------------------------------------------
    # 4. Gros Michel alone on last hand — baseline (no x3)
    # ---------------------------------------------------------------
    results.append(run_test(client, "Gros Michel alone: last hand", "ACR4",
        ["j_gros_michel"], hands_left=1))

    # ---------------------------------------------------------------
    # 5. Acrobat + Gros Michel NOT last hand — only Gros Michel fires
    # ---------------------------------------------------------------
    results.append(run_test(client, "Acrobat + Gros Michel: not last hand", "ACR5",
        ["j_gros_michel", "j_acrobat"], hands_left=3))

    # ---------------------------------------------------------------
    # 6. Dusk retrigger on last hand (hands_left=1) — retrigger all scored cards
    # ---------------------------------------------------------------
    results.append(run_test(client, "Dusk: last hand (hands=1)", "ACR6",
        ["j_dusk"], hands_left=1))

    # ---------------------------------------------------------------
    # 7. Dusk NOT last hand (hands_left=2) — no retrigger
    # ---------------------------------------------------------------
    results.append(run_test(client, "Dusk: not last hand (hands=2)", "ACR7",
        ["j_dusk"], hands_left=2))

    # ---------------------------------------------------------------
    # Summary
    # ---------------------------------------------------------------
    print(f"\n\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    for r in results:
        if r is None:
            continue
        status = "MATCH" if r["diff"] == 0 else f"MISMATCH({r['diff']:+d})"
        print(f"  {r['label']:50s} est={r['est']:>8d} actual={r['actual']:>8d} {status}")

    mismatches = [r for r in results if r and r["diff"] != 0]
    if mismatches:
        print(f"\nFAILED: {len(mismatches)} mismatch(es)")
        sys.exit(1)
    print("\nPASSED: all scores matched")


if __name__ == "__main__":
    main()
