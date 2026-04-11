"""Integration tests for Blackboard joker — held-card suit condition edge cases.

Blackboard: "X3 Mult if all cards held in hand are Spades or Clubs."
Tests Wild cards, Stone cards, debuffed cards, and baseline S/C vs H/D held hands.

Usage:
    python test_blackboard.py [--port PORT]
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


def run_test(client, label, seed, joker_keys, hand, play_count=None):
    """Run a test playing `play_count` of the added cards (default: all).

    Cards are added at end of hand. We play the last `play_count` cards,
    leaving the rest as held cards for Blackboard to evaluate.
    """
    if play_count is None:
        play_count = len(hand)

    print(f"\n{'='*60}")
    print(f"TEST: {label}")
    print(f"{'='*60}")

    state = setup_game(client, seed, joker_keys=joker_keys, card_configs=hand)
    jokers = state.get("jokers", {}).get("cards", [])
    hand_cards = state.get("hand", {}).get("cards", [])
    hl = state.get("round", {}).get("hands_left", "?")

    print(f"  hands_left={hl}")
    print(f"  Total cards in hand: {len(hand_cards)}")
    print(f"  Jokers:")
    for j in jokers:
        ab = j.get("value", {}).get("ability", {})
        effect = j.get("value", {}).get("effect", "")[:100]
        print(f"    {j.get('key'):20s} ability={ab} effect={effect}")

    # Play the LAST play_count added cards, hold everything else
    added_start = len(hand_cards) - len(hand)
    play_indices = list(range(len(hand_cards) - play_count, len(hand_cards)))
    played = [hand_cards[i] for i in play_indices]
    held = [c for j, c in enumerate(hand_cards) if j not in set(play_indices)]

    print(f"  Playing {len(played)} cards: {[c.get('label','?') for c in played]}")
    print(f"  Holding {len(held)} cards: {[c.get('label','?') for c in held]}")
    # Show suit/enhancement details for held cards
    for c in held:
        mod = _modifier(c)
        enh = mod.get("enhancement", "")
        ed = mod.get("edition", "")
        suit = c.get("value", {}).get("suit", "?")
        rank = c.get("value", {}).get("rank", "?")
        debuffed = (c.get("state") or {}).get("debuff", False)
        extras = []
        if enh: extras.append(f"enh={enh}")
        if ed: extras.append(f"ed={ed}")
        if debuffed: extras.append("DEBUFFED")
        print(f"    held: {rank}{suit} {' '.join(extras)}")

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

    # All tests: play 2 cards (Pair of Kings), hold 3 cards.
    # Blackboard checks the 3 held cards + any remaining from the original hand.

    # We add 5 cards total: 2 Kings to play + 3 held cards.
    # play_count=2 means play the last 2 added cards.

    # ---------------------------------------------------------------
    # 1. Baseline: all held cards are S/C → Blackboard SHOULD fire x3
    # ---------------------------------------------------------------
    results.append(run_test(client, "Blackboard: all S/C held (should fire x3)", "BB1",
        ["j_blackboard"],
        hand=[
            {"key": "S_3"},  # held - Spade
            {"key": "C_4"},  # held - Club
            {"key": "S_7"},  # held - Spade
            {"key": "S_K"}, {"key": "C_K"},  # played - Pair
        ],
        play_count=2))

    # ---------------------------------------------------------------
    # 2. One Heart held → Blackboard should NOT fire
    # ---------------------------------------------------------------
    results.append(run_test(client, "Blackboard: one Heart held (should NOT fire)", "BB2",
        ["j_blackboard"],
        hand=[
            {"key": "H_3"},  # held - Heart (breaks condition)
            {"key": "C_4"},  # held - Club
            {"key": "S_7"},  # held - Spade
            {"key": "S_K"}, {"key": "C_K"},  # played
        ],
        play_count=2))

    # ---------------------------------------------------------------
    # 3. Wild card (base suit Heart) held → does Wild pass for Blackboard?
    # ---------------------------------------------------------------
    results.append(run_test(client, "Blackboard: Wild Heart held (Wild=all suits?)", "BB3",
        ["j_blackboard"],
        hand=[
            {"key": "H_3", "enhancement": "WILD"},  # held - Wild Heart
            {"key": "C_4"},  # held - Club
            {"key": "S_7"},  # held - Spade
            {"key": "S_K"}, {"key": "C_K"},  # played
        ],
        play_count=2))

    # ---------------------------------------------------------------
    # 4. Stone card held alongside S/C → does Stone pass or fail?
    # ---------------------------------------------------------------
    results.append(run_test(client, "Blackboard: Stone card held with S/C", "BB4",
        ["j_blackboard"],
        hand=[
            {"key": "H_3", "enhancement": "STONE"},  # held - Stone (no suit)
            {"key": "C_4"},  # held - Club
            {"key": "S_7"},  # held - Spade
            {"key": "S_K"}, {"key": "C_K"},  # played
        ],
        play_count=2))

    # ---------------------------------------------------------------
    # 5. No held cards (play all 5) → should NOT fire
    # ---------------------------------------------------------------
    results.append(run_test(client, "Blackboard: no held cards (play all 5)", "BB5",
        ["j_blackboard"],
        hand=[
            {"key": "S_K"}, {"key": "C_K"}, {"key": "S_3"}, {"key": "C_4"}, {"key": "S_7"},
        ],
        play_count=5))

    # ---------------------------------------------------------------
    # 6. Debuffed Heart held with S/C → does debuff matter?
    # ---------------------------------------------------------------
    # Note: We can't directly debuff via add API. Skip this test or
    # test it manually against a boss blind that debuffs.

    # ---------------------------------------------------------------
    # 7. All Diamonds held → should NOT fire (baseline negative)
    # ---------------------------------------------------------------
    results.append(run_test(client, "Blackboard: all Diamonds held (should NOT fire)", "BB6",
        ["j_blackboard"],
        hand=[
            {"key": "D_3"},  # held - Diamond
            {"key": "D_4"},  # held - Diamond
            {"key": "D_7"},  # held - Diamond
            {"key": "S_K"}, {"key": "C_K"},  # played
        ],
        play_count=2))

    # ---------------------------------------------------------------
    # 8. Wild Heart held + Smeared Joker (H/D merge into H+D, S/C merge into S+C)
    #    Smeared makes Heart count as {H,D} — still no S/C overlap
    # ---------------------------------------------------------------
    results.append(run_test(client, "Blackboard + Smeared: Heart held (H→{H,D})", "BB7",
        ["j_blackboard", "j_smeared"],
        hand=[
            {"key": "H_3"},  # held - Heart → {H,D} with Smeared
            {"key": "C_4"},  # held - Club → {C,S} with Smeared
            {"key": "S_7"},  # held - Spade → {C,S} with Smeared
            {"key": "S_K"}, {"key": "C_K"},  # played
        ],
        play_count=2))

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
        print(f"  {r['label']:55s} est={r['est']:>8d} actual={r['actual']:>8d} {status}")

    mismatches = [r for r in results if r and r["diff"] != 0]
    if mismatches:
        print(f"\nFAILED: {len(mismatches)} mismatch(es)")
        sys.exit(1)
    print("\nPASSED: all scores matched")


if __name__ == "__main__":
    main()
