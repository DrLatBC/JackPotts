"""Integration tests for Shortcut joker edge cases.

Shortcut allows straights with gaps of 1 rank between adjacent cards
(i.e. every other rank: 2-4-6-8-T). Combined with Four Fingers, only
4 cards are needed for the straight, and SF flush/straight subsets can
differ.

Tests:
  sc_a: Basic shortcut straight (2-4-6-8-T, all diff suits)
  sc_b: Ace-low shortcut (A-2-4-6-8)
  sc_c: Ace-high shortcut with off-rank (8-T-Q-A + 3)
  sc_d: No wrapping (Q-K-A-2-4 should NOT be straight)
  sc_e: Shortcut + Four Fingers — 4-card gapped straight + off-rank
  sc_f: Shortcut + FF SF — wiki example Q♠ J♠ 9♥ 7♠ 3♠
  sc_g: Shortcut + FF SF — all suited gapped straight + flush-only card

Usage:
    python test_shortcut_edges.py --start-server
    python test_shortcut_edges.py --port 12346
"""

import argparse
import logging
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from balatrobot.cli.client import BalatroClient, APIError
from balatro_bot.domain.scoring.classify import classify_hand, _scoring_cards_for
from balatro_bot.domain.scoring.estimate import score_hand_detailed
from balatro_bot.cards import _modifier, card_rank, card_suits, is_joker_debuffed

from harness import (
    TEST_PORT,
    wait_for_state, setup_game_full as setup_clean,
    get_current_blind,
    ensure_server, stop_server, take_screenshot,
)
from scoring_diagnostics import fmt_card



def play_and_compare(client, state, play_indices, label="", blind_name=""):
    """Play cards at given indices, compare bot estimate vs actual."""
    hand_cards = state.get("hand", {}).get("cards", [])
    jokers = state.get("jokers", {}).get("cards", [])
    hand_levels = state.get("hands", {})

    played = [hand_cards[i] for i in play_indices if i < len(hand_cards)]
    held = [c for j, c in enumerate(hand_cards) if j not in set(play_indices)]

    joker_keys_set = {j.get("key") for j in jokers if not is_joker_debuffed(j)}
    four_fingers = "j_four_fingers" in joker_keys_set
    shortcut = "j_shortcut" in joker_keys_set
    smeared = "j_smeared" in joker_keys_set

    hand_name = classify_hand(
        played,
        four_fingers=four_fingers,
        shortcut=shortcut,
        smeared=smeared,
    )
    scoring = played if "j_splash" in joker_keys_set else _scoring_cards_for(
        hand_name, played, four_fingers=four_fingers, smeared=smeared,
        shortcut=shortcut)

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
        deck_cards=state.get("cards", {}).get("cards", []),
    )

    pre_chips = state.get("round", {}).get("chips", 0)

    played_str = ", ".join(fmt_card(c) for c in played)
    scoring_str = ", ".join(fmt_card(c) for c in scoring)
    print(f"\n  [{label}]")
    print(f"    Hand:    {hand_name}")
    print(f"    Played:  [{played_str}] ({len(played)} cards)")
    print(f"    Scoring: [{scoring_str}] ({len(scoring)} cards)")
    print(f"    Base: {detail['base_chips']}/{detail['base_mult']}")
    for entry in detail.get("joker_contributions", []):
        jlabel, dc, dm = entry[0], entry[1], entry[2]
        xm = entry[3] if len(entry) > 3 else 1.0
        parts = []
        if dc:
            parts.append(f"+{dc:.0f}c")
        if xm > 1.01 or xm < 0.99:
            parts.append(f"x{xm:.2f}")
        elif dm:
            parts.append(f"+{dm:.1f}m")
        if parts:
            print(f"      {jlabel}: {', '.join(parts)}")
    print(f"    Estimate: {detail['total']}")

    try:
        new_state = client.call("play", {"cards": play_indices})
    except APIError as e:
        print(f"    Play failed: {e.message}")
        return {"label": label, "est": detail["total"], "actual": 0, "diff": 0,
                "hand_name": hand_name, "error": True}

    post_chips = new_state.get("round", {}).get("chips", 0)
    actual = post_chips - pre_chips
    diff = actual - detail["total"]

    status = "MATCH" if diff == 0 else f"MISMATCH({diff:+d})"
    print(f"    Actual:   {actual}")
    print(f"    >> {status}")

    if diff != 0:
        try:
            take_screenshot(client, f"shortcut_{label}")
        except Exception:
            pass

    return {"label": label, "est": detail["total"], "actual": actual, "diff": diff,
            "hand_name": hand_name, "new_state": new_state}


# ---------------------------------------------------------------------------
# Helper to run a shortcut test case
# ---------------------------------------------------------------------------

def _run_sc_case(client, results, seed, label, card_configs, joker_keys,
                 expected_hand=None):
    """Set up jokers + inject cards, play last 5, compare."""
    print(f"\n{'='*60}")
    print(f"SC: {label}")
    print(f"{'='*60}")

    state = setup_clean(client, seed,
        joker_keys=joker_keys,
        card_configs=card_configs)

    hand_cards = state.get("hand", {}).get("cards", [])
    print(f"  Hand: {len(hand_cards)} cards")
    for i, c in enumerate(hand_cards):
        print(f"    [{i}] {fmt_card(c)}")

    play_indices = list(range(len(hand_cards) - 5, len(hand_cards)))
    print(f"  Playing indices: {play_indices}")
    r = play_and_compare(client, state, play_indices, label)

    if expected_hand and r["hand_name"] != expected_hand:
        print(f"    !! CLASSIFICATION: expected {expected_hand}, got {r['hand_name']}")
    results.append(r)


# ---------------------------------------------------------------------------
# Shortcut-only tests
# ---------------------------------------------------------------------------

def test_sc_basic_gapped(client, results):
    """SC-A: Basic gapped straight 2-4-6-8-T (all diff suits)."""
    _run_sc_case(client, results, "SC_A",
        "Straight: 2-4-6-8-T (gapped, all diff suits)",
        [{"key": "H_2"}, {"key": "D_4"}, {"key": "C_6"},
         {"key": "S_8"}, {"key": "H_T"}],
        joker_keys=["j_shortcut"],
        expected_hand="Straight")


def test_sc_ace_low(client, results):
    """SC-B: Ace-low gapped straight A-2-4-6-8."""
    _run_sc_case(client, results, "SC_B",
        "Straight: A-2-4-6-8 (ace low, gapped)",
        [{"key": "H_A"}, {"key": "D_2"}, {"key": "C_4"},
         {"key": "S_6"}, {"key": "H_8"}],
        joker_keys=["j_shortcut"],
        expected_hand="Straight")


def test_sc_ace_high_offrank(client, results):
    """SC-C: Ace-high gapped straight 8-T-Q-A + off-rank 3."""
    _run_sc_case(client, results, "SC_C",
        "Straight: 8-T-Q-A + 3 (ace high, off-rank)",
        [{"key": "H_8"}, {"key": "D_T"}, {"key": "C_Q"},
         {"key": "S_A"}, {"key": "H_3"}],
        joker_keys=["j_shortcut"],
        expected_hand="Straight")


def test_sc_no_wrap(client, results):
    """SC-D: No wrapping — Q-K-A-2-4 should NOT be straight.
    Should classify as High Card (no pair, no straight)."""
    _run_sc_case(client, results, "SC_D",
        "NOT Straight: Q-K-A-2-4 (wrapping, should fail)",
        [{"key": "H_Q"}, {"key": "D_K"}, {"key": "C_A"},
         {"key": "S_2"}, {"key": "H_4"}],
        joker_keys=["j_shortcut"],
        expected_hand="High Card")


# ---------------------------------------------------------------------------
# Shortcut + Four Fingers tests
# ---------------------------------------------------------------------------

def test_sc_ff_gapped_straight(client, results):
    """SC-E: Shortcut + FF — 4-card gapped straight (2-4-6-8) + off-rank K."""
    _run_sc_case(client, results, "SC_E",
        "SC+FF Straight: 2-4-6-8 + K (off-rank)",
        [{"key": "H_2"}, {"key": "D_4"}, {"key": "C_6"},
         {"key": "S_8"}, {"key": "H_K"}],
        joker_keys=["j_shortcut", "j_four_fingers"],
        expected_hand="Straight")


def test_sc_ff_sf_wiki(client, results):
    """SC-F: Wiki example — Q♠ J♠ 9♥ 7♠ 3♠ = SF with Shortcut+FF.
    Straight={Q,J,9,7} (gaps of 2), Flush={Q♠,J♠,7♠,3♠}. 9♥ is in straight only."""
    _run_sc_case(client, results, "SC_F",
        "SC+FF SF: QsJsS9h7s3s (wiki example)",
        [{"key": "S_Q"}, {"key": "S_J"}, {"key": "H_9"},
         {"key": "S_7"}, {"key": "S_3"}],
        joker_keys=["j_shortcut", "j_four_fingers"],
        expected_hand="Straight Flush")


def test_sc_ff_sf_flush_only(client, results):
    """SC-G: SC+FF SF — 4 suited gapped straight (4♥6♥8♥T♥) + flush-only K♥.
    Straight={4,6,8,T}, Flush={all hearts}. K♥ is flush-only."""
    _run_sc_case(client, results, "SC_G",
        "SC+FF SF: 4H 6H 8H TH KH (K flush-only)",
        [{"key": "H_4"}, {"key": "H_6"}, {"key": "H_8"},
         {"key": "H_T"}, {"key": "H_K"}],
        joker_keys=["j_shortcut", "j_four_fingers"],
        expected_hand="Straight Flush")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Integration tests for Shortcut joker edge cases")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--seed", type=str, default="SC01")
    parser.add_argument("--start-server", action="store_true")
    parser.add_argument("--test", type=str, default="all",
                        help="Run specific test: sc_a-sc_g, or 'all'")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)

    port = args.port or (TEST_PORT if args.start_server else 12346)
    server_proc = None

    if args.start_server:
        client, server_proc = ensure_server(port)
    else:
        client = BalatroClient(port=port)

    try:
        client.call("health")
    except Exception as e:
        print(f"No server on port {port}: {e}")
        sys.exit(1)

    results = []
    tests = {
        "sc_a": ("Shortcut: basic gapped 2-4-6-8-T", test_sc_basic_gapped),
        "sc_b": ("Shortcut: ace-low A-2-4-6-8", test_sc_ace_low),
        "sc_c": ("Shortcut: ace-high 8-T-Q-A + off-rank", test_sc_ace_high_offrank),
        "sc_d": ("Shortcut: no wrapping Q-K-A-2-4", test_sc_no_wrap),
        "sc_e": ("SC+FF: 4-card gapped straight + off-rank", test_sc_ff_gapped_straight),
        "sc_f": ("SC+FF SF: wiki example QsJs9h7s3s", test_sc_ff_sf_wiki),
        "sc_g": ("SC+FF SF: flush-only card", test_sc_ff_sf_flush_only),
    }

    if args.test == "all":
        run_tests = list(tests.keys())
    else:
        run_tests = [t.strip() for t in args.test.split(",")]
        for t in run_tests:
            if t not in tests:
                print(f"Unknown test '{t}'. Available: {', '.join(tests.keys())}")
                sys.exit(1)

    for tid in run_tests:
        label, func = tests[tid]
        print(f"\n\n{'#'*70}")
        print(f"# {label}")
        print(f"{'#'*70}")
        try:
            func(client, results)
        except Exception as e:
            print(f"\n  !! TEST FAILED WITH EXCEPTION: {e}")
            import traceback
            traceback.print_exc()
            results.append({"label": label, "est": 0, "actual": 0, "diff": -1,
                            "hand_name": "ERROR", "error": True})

    # --- Summary ---
    print(f"\n\n{'#'*70}")
    print(f"# SUMMARY")
    print(f"{'#'*70}")

    matches = 0
    mismatches = 0
    errors = 0
    for r in results:
        if r.get("error"):
            errors += 1
            status = "ERROR"
        elif r["diff"] == 0:
            matches += 1
            status = "MATCH"
        else:
            mismatches += 1
            status = f"MISMATCH({r['diff']:+d})"
        print(f"  {status:>16s}  {r['hand_name']:20s}  est={r['est']:>8d}  actual={r['actual']:>8d}  | {r['label']}")

    print(f"\n  Total: {len(results)} tests | {matches} MATCH | {mismatches} MISMATCH | {errors} ERROR")

    if mismatches > 0:
        print(f"\n  !! {mismatches} MISMATCHES DETECTED")
    elif errors > 0:
        print(f"\n  !! {errors} ERRORS — some tests could not complete")
    else:
        print(f"\n  ALL TESTS MATCH")

    if server_proc:
        stop_server(server_proc)


if __name__ == "__main__":
    main()
