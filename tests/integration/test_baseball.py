"""Isolate Baseball Card scoring: test with different joker combos to find root cause.

Usage:
    python test_baseball.py [--port PORT]
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
from balatro_bot.cards import card_chip_value, card_mult_value, _modifier

from harness import wait_for_state, setup_game_full as setup_game


def play_and_compare(client, state, play_indices, label=""):
    hand_cards = state.get("hand", {}).get("cards", [])
    jokers = state.get("jokers", {}).get("cards", [])
    hand_levels = state.get("hands", {})

    played = [hand_cards[i] for i in play_indices]
    held = [c for j, c in enumerate(hand_cards) if j not in set(play_indices)]

    hand_name = classify_hand(played)
    joker_keys = {j.get("key") for j in jokers}
    scoring = played if "j_splash" in joker_keys else _scoring_cards_for(hand_name, played)

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
    )

    pre_chips = state.get("round", {}).get("chips", 0)

    try:
        new_state = client.call("play", {"cards": play_indices})
    except APIError as e:
        print(f"  Play failed: {e.message}")
        return {"label": label, "est": detail["total"], "actual": 0, "diff": 0,
                "chips": detail["post_joker_chips"], "mult": detail["post_joker_mult"]}

    post_chips = new_state.get("round", {}).get("chips", 0)
    actual = post_chips - pre_chips
    diff = actual - detail["total"]
    status = "MATCH" if diff == 0 else f"MISMATCH({diff:+d})"

    return {"label": label, "est": detail["total"], "actual": actual, "diff": diff,
            "chips": detail["post_joker_chips"], "mult": detail["post_joker_mult"]}


# Same cards for every test — Pair of Aces + 3 plain non-face non-special cards
HAND = [{"key": "S_K"}, {"key": "H_K"}, {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"}]


def run_test(client, label, seed, joker_keys):
    print(f"\n{'='*60}")
    print(f"TEST: {label}")
    print(f"{'='*60}")

    state = setup_game(client, seed, joker_keys=joker_keys, card_configs=HAND)
    jokers = state.get("jokers", {}).get("cards", [])
    hand_cards = state.get("hand", {}).get("cards", [])

    print(f"  Jokers ({len(jokers)}):")
    for j in jokers:
        mod = j.get("modifier", {})
        if not isinstance(mod, dict): mod = {}
        rarity = j.get("value", {}).get("rarity")
        ab = j.get("value", {}).get("ability", {})
        effect = j.get("value", {}).get("effect", "")[:80]
        print(f"    {j.get('key'):20s} rarity={str(rarity):12s} edition={mod.get('edition','-'):12s} ability={ab}")

    print(f"  Hand ({len(hand_cards)} cards):")
    for i, c in enumerate(hand_cards):
        rank = c.get("value", {}).get("rank", "?")
        suit = c.get("value", {}).get("suit", "?")
        print(f"    [{i}] {rank}{suit} chips={card_chip_value(c)}")

    play_indices = list(range(len(hand_cards) - 5, len(hand_cards)))
    r = play_and_compare(client, state, play_indices, label)

    actual_mult = r["actual"] / r["chips"] if r["chips"] else 0
    print(f"  Bot:    {r['chips']:.0f} chips x {r['mult']:.1f} mult = {r['est']}")
    print(f"  Actual: {r['chips']:.0f} chips x {actual_mult:.1f} mult = {r['actual']}")
    print(f"  Diff: {r['diff']:+d}  {'MATCH' if r['diff'] == 0 else 'MISMATCH'}")
    if r["diff"] != 0 and r["chips"]:
        print(f"  Mult gap: {actual_mult - r['mult']:+.1f}")

    return r


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
    # Baseline: no jokers at all
    # ---------------------------------------------------------------
    results.append(run_test(client, "No jokers (baseline)", "BB0", []))

    # ---------------------------------------------------------------
    # Baseball alone — does it do anything with 0 uncommon?
    # ---------------------------------------------------------------
    results.append(run_test(client, "Baseball alone (0 uncommon)", "BB1", ["j_baseball"]))

    # ---------------------------------------------------------------
    # Baseball + 1 known Uncommon (Fibonacci)
    # ---------------------------------------------------------------
    results.append(run_test(client, "Baseball + Fibonacci (1 unc)", "BB2",
                            ["j_baseball", "j_fibonacci"]))

    # ---------------------------------------------------------------
    # Fibonacci alone — isolate Fibonacci contribution
    # ---------------------------------------------------------------
    results.append(run_test(client, "Fibonacci alone (no baseball)", "BB3",
                            ["j_fibonacci"]))

    # ---------------------------------------------------------------
    # Baseball + 1 known Common (Jolly Joker) — should NOT trigger
    # ---------------------------------------------------------------
    results.append(run_test(client, "Baseball + Jolly (common, no trigger)", "BB4",
                            ["j_baseball", "j_jolly"]))

    # ---------------------------------------------------------------
    # Baseball + Jolly + Fibonacci — 1 unc (Fib), 1 common (Jolly)
    # ---------------------------------------------------------------
    results.append(run_test(client, "Baseball + Jolly + Fibonacci", "BB5",
                            ["j_baseball", "j_jolly", "j_fibonacci"]))

    # ---------------------------------------------------------------
    # Baseball + Scary Face (Uncommon in vanilla) — test rarity
    # ---------------------------------------------------------------
    results.append(run_test(client, "Baseball + Scary Face", "BB6",
                            ["j_baseball", "j_scary_face"]))

    # ---------------------------------------------------------------
    # Baseball + Smiley Face (Common in vanilla)
    # ---------------------------------------------------------------
    results.append(run_test(client, "Baseball + Smiley Face", "BB7",
                            ["j_baseball", "j_smiley"]))

    # ---------------------------------------------------------------
    # Baseball + 2 Uncommon (Scary Face + Fibonacci)
    # ---------------------------------------------------------------
    results.append(run_test(client, "Baseball + Scary + Fibonacci (2 unc)", "BB8",
                            ["j_baseball", "j_scary_face", "j_fibonacci"]))

    # ---------------------------------------------------------------
    # Baseball + Even Steven + Odd Todd — check their actual rarity
    # ---------------------------------------------------------------
    results.append(run_test(client, "Baseball + Even Steven + Odd Todd", "BB9",
                            ["j_baseball", "j_even_steven", "j_odd_todd"]))

    # ---------------------------------------------------------------
    # Even Steven alone — isolate its contribution (no baseball)
    # ---------------------------------------------------------------
    results.append(run_test(client, "Even Steven alone", "BB10",
                            ["j_even_steven"]))

    # ---------------------------------------------------------------
    # Odd Todd alone — isolate its contribution
    # ---------------------------------------------------------------
    results.append(run_test(client, "Odd Todd alone", "BB11",
                            ["j_odd_todd"]))

    # ---------------------------------------------------------------
    # Summary
    # ---------------------------------------------------------------
    print(f"\n\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    print(f"  {'Label':50s} {'Est':>8s} {'Actual':>8s} {'Diff':>8s} {'MultGap':>8s}")
    print(f"  {'-'*50} {'-'*8} {'-'*8} {'-'*8} {'-'*8}")
    for r in results:
        if r is None:
            continue
        actual_mult = r["actual"] / r["chips"] if r["chips"] else 0
        mg = actual_mult - r["mult"] if r["chips"] else 0
        status = "" if r["diff"] == 0 else f"{mg:+.1f}"
        print(f"  {r['label']:50s} {r['est']:8d} {r['actual']:8d} {r['diff']:+8d} {status:>8s}")

    mismatches = [r for r in results if r and r["diff"] != 0]
    if mismatches:
        print(f"\nFAILED: {len(mismatches)} mismatch(es)")
        sys.exit(1)
    print("\nPASSED: all scores matched")


if __name__ == "__main__":
    main()
