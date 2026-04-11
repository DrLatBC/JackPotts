"""Integration tests for Cavendish joker — 63% mismatch rate, 5.9x lift.

Cavendish: static X3 Mult (no conditions, no scaling). Should be trivial.
Tests: alone, with other jokers, different hand types.
Dumps raw ability/effect/parsed data to find the discrepancy.

Usage:
    python test_cavendish.py [--port PORT]
"""

import argparse
import json
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
from balatro_bot.joker_effects.parsers import parse_effect_value, _ability, _ab_mult, _ab_xmult
from harness import wait_for_state, setup_game_full as setup_game


def dump_jokers(jokers):
    for j in jokers:
        key = j.get("key", "?")
        ab = j.get("value", {}).get("ability", {})
        effect = j.get("value", {}).get("effect", "")
        parsed = parse_effect_value(effect)
        mod = j.get("modifier", {})
        print(f"    {key:20s}")
        print(f"      ability  = {ab}")
        print(f"      effect   = {effect[:140]}")
        print(f"      parsed   = {parsed}")
        print(f"      modifier = {json.dumps(mod) if isinstance(mod, dict) else mod}")
        print(f"      _ab_xmult = {_ab_xmult(j, fallback=-999)}")
        print(f"      _ab_mult  = {_ab_mult(j, fallback=-999)}")


def run_test(client, label, seed, joker_keys, hand, play_count=None):
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
        hands_left=state.get("round", {}).get("hands_left", 4),
        joker_limit=state.get("jokers", {}).get("limit", 5),
    )

    pre_chips = state.get("round", {}).get("chips", 0)
    print(f"  Hand: {hand_name}")
    print(f"  Playing: {[c.get('label','?') for c in played]}")
    print(f"  Holding: {len(held)} cards")
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

    pair_hand = [{"key": "S_K"}, {"key": "H_K"}, {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"}]
    high_card = [{"key": "S_A"}, {"key": "H_7"}, {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"}]

    # 1. Cavendish alone — Pair
    results.append(run_test(client, "Cavendish alone: Pair", "CAV1",
        ["j_cavendish"], pair_hand))

    # 2. Cavendish alone — High Card
    results.append(run_test(client, "Cavendish alone: High Card", "CAV2",
        ["j_cavendish"], high_card))

    # 3. Cavendish + Gros Michel (the other banana)
    results.append(run_test(client, "Cavendish + Gros Michel: Pair", "CAV3",
        ["j_cavendish", "j_gros_michel"], pair_hand))

    # 4. Cavendish + a scaling joker (common combo in real runs)
    results.append(run_test(client, "Cavendish + Green Joker: Pair", "CAV4",
        ["j_cavendish", "j_green_joker"], pair_hand))

    # 5. Cavendish with HOLO edition (edition interaction?)
    results.append(run_test(client, "Cavendish HOLO: Pair", "CAV5",
        [{"key": "j_cavendish", "edition": "HOLO"}], pair_hand))

    # 6. Cavendish with Polychrome edition
    results.append(run_test(client, "Cavendish Polychrome: Pair", "CAV6",
        [{"key": "j_cavendish", "edition": "POLYCHROME"}], pair_hand))

    # 7. Cavendish with Foil edition
    results.append(run_test(client, "Cavendish Foil: Pair", "CAV7",
        [{"key": "j_cavendish", "edition": "FOIL"}], pair_hand))

    # 8. Multiple xmult jokers — ordering matters
    results.append(run_test(client, "Cavendish + Jolly + Zany: Pair", "CAV8",
        ["j_jolly", "j_cavendish", "j_zany"], pair_hand))

    # 9. Cavendish second hand (after one play) — does ability change?
    print(f"\n{'='*60}")
    print("TEST: Cavendish: second hand in round")
    print(f"{'='*60}")
    state = setup_game(client, "CAV9", joker_keys=["j_cavendish"],
        card_configs=[{"key": "S_K"}, {"key": "H_K"}, {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"},
                      {"key": "S_Q"}, {"key": "H_Q"}, {"key": "D_6"}, {"key": "C_7"}, {"key": "S_8"}])
    hand_cards = state.get("hand", {}).get("cards", [])
    jokers = state.get("jokers", {}).get("cards", [])

    # Play 1
    play1_idx = list(range(len(hand_cards) - 10, len(hand_cards) - 5))
    played1 = [hand_cards[i] for i in play1_idx]
    held1 = [c for j, c in enumerate(hand_cards) if j not in set(play1_idx)]
    hn1 = classify_hand(played1)
    sc1 = _scoring_cards_for(hn1, played1)
    d1 = score_hand_detailed(hn1, sc1, hand_levels=state.get("hands", {}),
        jokers=jokers, played_cards=played1, held_cards=held1,
        money=state.get("money", 0),
        discards_left=state.get("round", {}).get("discards_left", 0),
        hands_left=state.get("round", {}).get("hands_left", 4),
        joker_limit=state.get("jokers", {}).get("limit", 5))
    pre1 = state.get("round", {}).get("chips", 0)
    print(f"  Play 1: {hn1}, est={d1['total']}")
    dump_jokers(jokers)
    ns1 = client.call("play", {"cards": play1_idx})
    a1 = ns1.get("round", {}).get("chips", 0) - pre1
    diff1 = a1 - d1["total"]
    print(f"  Actual={a1}, Diff={diff1:+d} {'MATCH' if diff1 == 0 else 'MISMATCH'}")

    time.sleep(0.5)
    state2 = wait_for_state(client, ["SELECTING_HAND"])
    jokers2 = state2.get("jokers", {}).get("cards", [])
    hand_cards2 = state2.get("hand", {}).get("cards", [])

    # Play 2 — check if Cavendish ability changed
    play2_idx = list(range(len(hand_cards2) - 5, len(hand_cards2)))
    played2 = [hand_cards2[i] for i in play2_idx]
    held2 = [c for j, c in enumerate(hand_cards2) if j not in set(play2_idx)]
    hn2 = classify_hand(played2)
    sc2 = _scoring_cards_for(hn2, played2)
    d2 = score_hand_detailed(hn2, sc2, hand_levels=state2.get("hands", {}),
        jokers=jokers2, played_cards=played2, held_cards=held2,
        money=state2.get("money", 0),
        discards_left=state2.get("round", {}).get("discards_left", 0),
        hands_left=state2.get("round", {}).get("hands_left", 3),
        joker_limit=state2.get("jokers", {}).get("limit", 5))
    pre2 = state2.get("round", {}).get("chips", 0)
    print(f"\n  Play 2: {hn2}, est={d2['total']}")
    dump_jokers(jokers2)
    ns2 = client.call("play", {"cards": play2_idx})
    a2 = ns2.get("round", {}).get("chips", 0) - pre2
    diff2 = a2 - d2["total"]
    print(f"  Actual={a2}, Diff={diff2:+d} {'MATCH' if diff2 == 0 else 'MISMATCH'}")

    results.append({"label": "Cavendish: play 1", "est": d1["total"], "actual": a1, "diff": diff1})
    results.append({"label": "Cavendish: play 2", "est": d2["total"], "actual": a2, "diff": diff2})

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
