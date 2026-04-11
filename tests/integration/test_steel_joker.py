"""Integration tests for Steel Joker scoring accuracy.

Steel Joker gives X Mult equal to (1 + 0.2 * steel_card_count) where
steel_card_count is the number of Steel-enhanced cards in the FULL deck
(draw pile + hand + played). The game computes this dynamically at scoring
time by iterating G.playing_cards.

Theory: the bot reads a stale/incorrect xmult from the API (either from
ability.x_mult or parsed effect text) instead of counting Steel cards itself.
This test verifies by:
  1. Adding Steel Joker + a known number of Steel cards
  2. Playing a hand and comparing est vs actual
  3. Varying the Steel card count to confirm the formula

Usage:
    python test_steel_joker.py [--port PORT]
"""

import argparse
import logging
import math
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "support"))

from balatrobot.cli.client import BalatroClient, APIError
from balatro_bot.domain.scoring.classify import classify_hand, _scoring_cards_for
from balatro_bot.domain.scoring.estimate import score_hand_detailed
from balatro_bot.cards import card_chip_value, _modifier
from balatro_bot.joker_effects.parsers import parse_effect_value, _ability, _ab_xmult
from harness import wait_for_state, setup_game_full as setup_game


def dump_jokers(jokers):
    for j in jokers:
        key = j.get("key", "?")
        ab = j.get("value", {}).get("ability", {})
        effect = j.get("value", {}).get("effect", "")
        parsed = parse_effect_value(effect)
        xmult_read = _ab_xmult(j, fallback=1.2)
        print(f"    {key:20s}")
        print(f"      ability  = {ab}")
        print(f"      effect   = {effect[:140]}")
        print(f"      parsed   = {parsed}")
        print(f"      _ab_xmult= {xmult_read}")


def count_steel_in_state(state):
    """Count Steel-enhanced cards across the full deck visible in state."""
    count = 0
    sources = {
        "hand": state.get("hand", {}).get("cards", []),
        "deck": state.get("cards", {}).get("cards", []),
    }
    for source_name, cards in sources.items():
        for c in cards:
            m = c.get("modifier", {})
            if isinstance(m, dict) and m.get("enhancement") == "STEEL":
                count += 1
    return count


def play_and_compare(client, state, play_count=5, label=""):
    jokers = state.get("jokers", {}).get("cards", [])
    hand_cards = state.get("hand", {}).get("cards", [])
    hand_levels = state.get("hands", {})

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
    )

    # What the bot thinks Steel Joker's xmult is
    steel_joker = next((j for j in jokers if j.get("key") == "j_steel_joker"), None)
    bot_xmult = _ab_xmult(steel_joker, fallback=1.2) if steel_joker else None

    # What it SHOULD be based on Steel card count
    steel_count = count_steel_in_state(state)
    expected_xmult = 1 + 0.2 * steel_count

    pre_chips = state.get("round", {}).get("chips", 0)
    print(f"  Hand: {hand_name}")
    print(f"  Playing: {[c.get('label','?') for c in played]}")
    print(f"  Steel cards in full deck: {steel_count}")
    print(f"  Bot reads Steel Joker xmult: {bot_xmult}")
    print(f"  Expected Steel Joker xmult: {expected_xmult} (1 + 0.2 * {steel_count})")
    print(f"  Base: {detail['base_chips']}/{detail['base_mult']}")
    print(f"  Post-joker: {detail['post_joker_chips']}/{detail['post_joker_mult']:.2f}")
    print(f"  Bot estimate: {detail['total']}")

    for entry in detail.get("joker_contributions", []):
        jlabel, dc, dm = entry[0], entry[1], entry[2]
        xm = entry[3] if len(entry) > 3 else 1.0
        parts = []
        if dc: parts.append(f"+{dc:.0f}c")
        if xm > 1.01 or xm < 0.99: parts.append(f"x{xm:.2f}")
        elif dm: parts.append(f"+{dm:.1f}m")
        if parts: print(f"    {jlabel}: {', '.join(parts)}")

    try:
        new_state = client.call("play", {"cards": play_indices})
    except APIError as e:
        print(f"  Play failed: {e.message}")
        return None

    post_chips = new_state.get("round", {}).get("chips", 0)
    actual = post_chips - pre_chips
    diff = actual - detail["total"]

    # What score would be with correctly computed xmult
    if bot_xmult and bot_xmult != expected_xmult and bot_xmult > 0:
        corrected_est = math.floor(detail["total"] * expected_xmult / bot_xmult)
        corrected_diff = actual - corrected_est
        print(f"  Corrected estimate (using x{expected_xmult}): {corrected_est}  diff={corrected_diff:+d}")

    print(f"  Actual: {actual}  Diff: {diff:+d}  {'MATCH' if diff == 0 else 'MISMATCH'}")
    if diff != 0 and bot_xmult != expected_xmult:
        print(f"  >>> THEORY: bot used x{bot_xmult}, game used x{expected_xmult}")
        if actual > 0:
            implied_xmult = actual / (detail["total"] / bot_xmult) if bot_xmult else 0
            print(f"  >>> Implied actual xmult: {implied_xmult:.3f}")

    return {
        "label": label, "est": detail["total"], "actual": actual, "diff": diff,
        "hand": hand_name, "steel_count": steel_count,
        "bot_xmult": bot_xmult, "expected_xmult": expected_xmult,
    }


def run_steel_joker_tests(client):
    """Test Steel Joker with varying numbers of Steel cards.

    Steel Joker: X Mult = 1 + 0.2 * (Steel cards in full deck)
    - 0 Steel cards → x1.0 (no effect)
    - 2 Steel cards → x1.4
    - 4 Steel cards → x1.8
    - 6 Steel cards → x2.2
    """
    print("\n" + "=" * 60)
    print("STEEL JOKER TESTS — dynamic deck counting")
    print("=" * 60)

    results = []

    # We play simple hands (Pair of Kings) with Steel Joker only.
    # Non-Steel cards in hand so Steel Joker xmult is the only variable.
    base_hand = [
        {"key": "S_K"}, {"key": "H_K"},
        {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"}
    ]

    # ---------------------------------------------------------------
    # Test 1: Steel Joker + 0 Steel cards in deck
    # Expected xmult: 1.0 (no effect)
    # ---------------------------------------------------------------
    print(f"\n--- Steel Joker + 0 Steel cards → x1.0 ---")
    state = setup_game(client, "STL1",
        joker_keys=["j_steel_joker"],
        card_configs=base_hand)
    dump_jokers(state.get("jokers", {}).get("cards", []))
    r = play_and_compare(client, state, label="0 Steel → x1.0")
    if r: results.append(r)

    # ---------------------------------------------------------------
    # Test 2: Steel Joker + 2 Steel cards (in hand, non-scoring)
    # Expected xmult: 1.4
    # Steel cards are in the junk positions (not part of the pair)
    # ---------------------------------------------------------------
    print(f"\n--- Steel Joker + 2 Steel cards → x1.4 ---")
    hand_2steel = [
        {"key": "S_K"}, {"key": "H_K"},
        {"key": "D_3", "enhancement": "STEEL"},
        {"key": "C_4", "enhancement": "STEEL"},
        {"key": "S_5"}
    ]
    state = setup_game(client, "STL2",
        joker_keys=["j_steel_joker"],
        card_configs=hand_2steel)
    dump_jokers(state.get("jokers", {}).get("cards", []))
    r = play_and_compare(client, state, label="2 Steel → x1.4")
    if r: results.append(r)

    # ---------------------------------------------------------------
    # Test 3: Steel Joker + 4 Steel cards (2 in hand, 2 added to deck)
    # Expected xmult: 1.8
    # ---------------------------------------------------------------
    print(f"\n--- Steel Joker + 4 Steel cards → x1.8 ---")
    hand_2steel_deck = [
        {"key": "S_K"}, {"key": "H_K"},
        {"key": "D_3", "enhancement": "STEEL"},
        {"key": "C_4", "enhancement": "STEEL"},
        {"key": "S_5"}
    ]
    # Add 2 more Steel cards to the deck (they'll be in draw pile)
    deck_steel = [
        {"key": "H_7", "enhancement": "STEEL"},
        {"key": "D_8", "enhancement": "STEEL"},
    ]
    state = setup_game(client, "STL3",
        joker_keys=["j_steel_joker"],
        card_configs=hand_2steel_deck + deck_steel)
    dump_jokers(state.get("jokers", {}).get("cards", []))
    steel_count = count_steel_in_state(state)
    print(f"  Steel cards visible in state: {steel_count}")
    r = play_and_compare(client, state, label="4 Steel → x1.8")
    if r: results.append(r)

    # ---------------------------------------------------------------
    # Test 4: Steel Joker + 5 Steel cards (all hand cards Steel)
    # Expected xmult: 2.0
    # Note: Steel cards in scoring positions also count
    # ---------------------------------------------------------------
    print(f"\n--- Steel Joker + 5 Steel cards (full hand) → x2.0 ---")
    all_steel = [
        {"key": "S_K", "enhancement": "STEEL"},
        {"key": "H_K", "enhancement": "STEEL"},
        {"key": "D_3", "enhancement": "STEEL"},
        {"key": "C_4", "enhancement": "STEEL"},
        {"key": "S_5", "enhancement": "STEEL"}
    ]
    state = setup_game(client, "STL4",
        joker_keys=["j_steel_joker"],
        card_configs=all_steel)
    dump_jokers(state.get("jokers", {}).get("cards", []))
    r = play_and_compare(client, state, label="5 Steel → x2.0")
    if r: results.append(r)

    # ---------------------------------------------------------------
    # Test 5: Steel Joker + 2 Steel cards + regular Joker
    # Tests that Steel count is independent of other joker effects
    # Expected Steel xmult: 1.4
    # ---------------------------------------------------------------
    print(f"\n--- Steel Joker + regular Joker + 2 Steel → x1.4 ---")
    state = setup_game(client, "STL5",
        joker_keys=["j_steel_joker", "j_joker"],
        card_configs=hand_2steel)
    dump_jokers(state.get("jokers", {}).get("cards", []))
    r = play_and_compare(client, state, label="2 Steel + Joker → x1.4 on Steel Joker")
    if r: results.append(r)

    # ---------------------------------------------------------------
    # Test 6: Steel Joker + 3 Steel cards in deck only (not in hand)
    # Tests that draw pile Steel cards are counted
    # Expected xmult: 1.6
    # ---------------------------------------------------------------
    print(f"\n--- Steel Joker + 3 Steel in deck only → x1.6 ---")
    deck_only_steel = [
        {"key": "H_2", "enhancement": "STEEL"},
        {"key": "D_6", "enhancement": "STEEL"},
        {"key": "C_9", "enhancement": "STEEL"},
    ]
    state = setup_game(client, "STL6",
        joker_keys=["j_steel_joker"],
        card_configs=base_hand + deck_only_steel)
    dump_jokers(state.get("jokers", {}).get("cards", []))
    steel_count = count_steel_in_state(state)
    print(f"  Steel cards visible in state: {steel_count}")
    r = play_and_compare(client, state, label="3 Steel in deck → x1.6")
    if r: results.append(r)

    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=12346)
    args = parser.parse_args()

    client = BalatroClient("localhost", args.port)
    client.timeout = 20

    results = run_steel_joker_tests(client)

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    matches = sum(1 for r in results if r["diff"] == 0)
    mismatches = sum(1 for r in results if r["diff"] != 0)
    print(f"  {matches} MATCH, {mismatches} MISMATCH out of {len(results)} tests\n")

    for r in results:
        status = "MATCH" if r["diff"] == 0 else "MISMATCH"
        xm_note = ""
        if r.get("bot_xmult") != r.get("expected_xmult"):
            xm_note = f" (bot=x{r['bot_xmult']}, expected=x{r['expected_xmult']})"
        print(f"  [{status:8s}] {r['label']:45s} est={r['est']:6d} actual={r['actual']:6d} diff={r['diff']:+d}{xm_note}")

    mismatches = [r for r in results if r and r["diff"] != 0]
    if mismatches:
        print(f"\nFAILED: {len(mismatches)} mismatch(es)")
        sys.exit(1)
    print("\nPASSED: all scores matched")


if __name__ == "__main__":
    main()
