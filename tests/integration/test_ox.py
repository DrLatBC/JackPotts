"""Integration tests for The Ox + Bull scoring.

Verifies that our scoring estimate correctly handles The Ox's money zeroing
by reading most_played_poker_hand from the API.

Tests:
  ox_a: Play the locked hand type — Bull should get $0
  ox_b: Play a different hand type — Bull should keep money

Usage:
    python test_ox.py --start-server
    python test_ox.py --port 12346
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
from balatro_bot.cards import is_joker_debuffed

from harness import (
    TEST_PORT,
    wait_for_state, get_current_blind,
    advance_to_boss_select, force_boss,
    beat_blind_fast, cheat_win_if_needed,
    ensure_server, stop_server, take_screenshot,
)
from scoring_diagnostics import fmt_card


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def setup_clean(client, seed, joker_keys=None, card_configs=None):
    """Start fresh game, sell default jokers, discard hand, inject jokers + cards."""
    try:
        client.call("menu")
    except Exception:
        pass
    time.sleep(0.5)

    client.call("start", {"deck": "RED", "stake": "WHITE", "seed": seed})
    state = wait_for_state(client, {"SELECTING_HAND"})

    for _ in range(state.get("jokers", {}).get("count", 0)):
        try:
            client.call("sell", {"joker": 0})
        except APIError:
            pass

    for _ in range(2):
        state = client.call("gamestate")
        hc = state.get("hand", {}).get("cards", [])
        if hc:
            try:
                client.call("discard", {"cards": list(range(min(len(hc), 5)))})
                time.sleep(0.2)
            except APIError:
                pass

    if joker_keys:
        for jk in joker_keys:
            params = {"key": jk} if isinstance(jk, str) else jk
            try:
                client.call("add", params)
            except APIError as e:
                print(f"  FAILED joker {params}: {e.message}")

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

    time.sleep(0.3)
    return client.call("gamestate")


def play_and_compare(client, state, play_indices, label="", blind_name="",
                     ox_most_played=None):
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

    hand_name = classify_hand(played, four_fingers=four_fingers,
                              shortcut=shortcut, smeared=smeared)
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
        ox_most_played=ox_most_played,
    )

    pre_chips = state.get("round", {}).get("chips", 0)

    played_str = ", ".join(fmt_card(c) for c in played)
    scoring_str = ", ".join(fmt_card(c) for c in scoring)
    print(f"\n  [{label}]")
    print(f"    Hand:    {hand_name}")
    print(f"    Played:  [{played_str}] ({len(played)} cards)")
    print(f"    Scoring: [{scoring_str}] ({len(scoring)} cards)")
    print(f"    Money:   ${state.get('money', '?')}")
    print(f"    Ox locked: {ox_most_played}")
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
            take_screenshot(client, f"ox_{label}")
        except Exception:
            pass

    return {"label": label, "est": detail["total"], "actual": actual, "diff": diff,
            "hand_name": hand_name, "new_state": new_state}


def play_injected_hand(client, card_keys):
    """Discard current hand, inject cards, play them."""
    state = client.call("gamestate")
    hc = state.get("hand", {}).get("cards", [])
    if hc:
        try:
            client.call("discard", {"cards": list(range(min(len(hc), 5)))})
            time.sleep(0.2)
        except APIError:
            pass
    for key in card_keys:
        try:
            client.call("add", {"key": key})
        except APIError:
            pass
    time.sleep(0.3)
    state = client.call("gamestate")
    hc = state.get("hand", {}).get("cards", [])
    indices = list(range(len(hc) - len(card_keys), len(hc)))
    try:
        client.call("play", {"cards": indices})
    except APIError:
        pass
    time.sleep(0.5)
    # Wait for next state
    for _ in range(20):
        state = client.call("gamestate")
        if state.get("state") in ("SELECTING_HAND", "BLIND_SELECT", "SHOP", "GAME_OVER"):
            break
        time.sleep(0.3)
    return state


def advance_to_ox(client):
    """Play Pair hands at ante 5 to lock Pair as most-played, then advance to The Ox at ante 6.

    The game sets most_played_poker_hand after defeating the previous boss,
    so we need to play Pairs during ante 5 (SB + BB) then beat ante 5's boss.
    That locks Pair before entering ante 6.
    """
    # Fast-forward to ante 5 SB
    print("  Advancing to ante 5...")
    state = advance_to_boss_select(client, target_ante=5)
    # We're at ante 5 boss select — beat the boss to get to ante 5 shops/ante 6
    # But first we need to play Pairs during ante 5. We already beat SB+BB via
    # advance_to_boss_select. Need to go back — let's just set ante to 5 and
    # play from SB.

    # Actually: advance_to_boss_select already beat SB+BB for ante 5.
    # The most_played updates after each blind. So we need to play Pairs
    # BEFORE advance does its thing. Let's take a different approach:
    # set ante to 5, play 3 pairs on SB, cheat win, play 3 pairs on BB, cheat win,
    # then beat boss fast. That locks Pair as most-played entering ante 6.

    # Reset: go back to menu and start fresh with ante set to 5
    try:
        client.call("menu")
    except Exception:
        pass
    time.sleep(0.5)
    client.call("start", {"deck": "RED", "stake": "WHITE", "seed": "OX_LOCK"})
    state = wait_for_state(client, {"SELECTING_HAND"})

    # Sell default jokers
    for _ in range(state.get("jokers", {}).get("count", 0)):
        try:
            client.call("sell", {"joker": 0})
        except APIError:
            pass

    # Set to ante 5
    try:
        client.call("set", {"ante": 5})
    except APIError:
        pass
    time.sleep(0.3)

    # Play 3 Pairs on small blind
    print("  Playing 3 Pairs on ante 5 SB...")
    for i in range(3):
        state = client.call("gamestate")
        if state.get("state") != "SELECTING_HAND":
            break
        play_injected_hand(client, ["H_K", "D_K", "C_3", "S_5", "H_7"])

    # Cheat to win SB
    blind_name, _ = get_current_blind(client.call("gamestate"))
    cheat_win_if_needed(client, blind_name)

    # Advance through post-blind (shop etc) to BB
    from harness import advance_through_post_blind
    state = advance_through_post_blind(client)
    if state.get("state") == "BLIND_SELECT":
        client.call("select")
        time.sleep(0.3)
    state = wait_for_state(client, {"SELECTING_HAND"})

    # Play 3 Pairs on big blind
    print("  Playing 3 Pairs on ante 5 BB...")
    for i in range(3):
        state = client.call("gamestate")
        if state.get("state") != "SELECTING_HAND":
            break
        play_injected_hand(client, ["H_K", "D_K", "C_3", "S_5", "H_7"])

    # Cheat to win BB
    blind_name, _ = get_current_blind(client.call("gamestate"))
    cheat_win_if_needed(client, blind_name)

    # Advance to boss select, beat boss fast to lock most_played entering ante 6
    state = advance_through_post_blind(client)
    if state.get("state") == "BLIND_SELECT":
        client.call("select")
        time.sleep(0.3)
    state = wait_for_state(client, {"SELECTING_HAND"})
    print("  Beating ante 5 boss fast...")
    beat_blind_fast(client, state)

    # Now advance through ante 6 SB+BB fast to reach boss select
    print("  Fast-forwarding ante 6 to boss select...")
    state = advance_through_post_blind(client)
    # Ante 6 SB
    if state.get("state") == "BLIND_SELECT":
        client.call("select")
        time.sleep(0.3)
    state = wait_for_state(client, {"SELECTING_HAND"})
    beat_blind_fast(client, state)
    state = advance_through_post_blind(client)
    # Ante 6 BB
    if state.get("state") == "BLIND_SELECT":
        client.call("select")
        time.sleep(0.3)
    state = wait_for_state(client, {"SELECTING_HAND"})
    beat_blind_fast(client, state)
    state = advance_through_post_blind(client)

    # Now at ante 6 boss select — force The Ox
    if state.get("state") != "BLIND_SELECT":
        # Wait for it
        for _ in range(20):
            state = client.call("gamestate")
            if state.get("state") == "BLIND_SELECT":
                break
            time.sleep(0.3)

    if not force_boss(client, "The Ox"):
        print("  SKIP: Could not force The Ox")
        return None

    client.call("select")
    time.sleep(0.3)
    state = wait_for_state(client, {"SELECTING_HAND"})
    return state


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_ox_locked_hand(client, results):
    """Play the Ox's locked hand type — Bull should use $0 money."""
    print(f"\n{'='*60}")
    print("OX-A: Play locked hand type — money zeroed")
    print(f"{'='*60}")

    state = setup_clean(client, "OX_A")
    state = advance_to_ox(client)
    if state is None:
        return

    # Read what the game locked
    ox_locked = state.get("round", {}).get("most_played_poker_hand")
    blind_name, _ = get_current_blind(state)
    print(f"  Blind: {blind_name}")
    print(f"  Ox locked hand: {ox_locked}")

    # Sell jokers, add Bull
    for _ in range(state.get("jokers", {}).get("count", 0)):
        try:
            client.call("sell", {"joker": 0})
        except APIError:
            pass
    try:
        client.call("add", {"key": "j_bull"})
    except APIError as e:
        print(f"  FAILED adding bull: {e.message}")

    # Set known money
    try:
        client.call("set", {"money": 200})
    except APIError:
        pass

    # Inject cards that make the locked hand type
    # Map locked hand to card injection
    hand_to_cards = {
        "Pair": ["H_K", "D_K", "C_3", "S_5", "H_7"],
        "High Card": ["H_2", "D_5", "C_8", "S_J", "H_A"],
        "Two Pair": ["H_K", "D_K", "C_9", "S_9", "H_3"],
        "Three of a Kind": ["H_K", "D_K", "C_K", "S_5", "H_7"],
    }
    inject_cards = hand_to_cards.get(ox_locked, hand_to_cards["Pair"])
    print(f"  Injecting cards for {ox_locked}: {inject_cards}")

    state = client.call("gamestate")
    hc = state.get("hand", {}).get("cards", [])
    if hc:
        try:
            client.call("discard", {"cards": list(range(min(len(hc), 5)))})
            time.sleep(0.2)
        except APIError:
            pass
    for key in inject_cards:
        try:
            client.call("add", {"key": key})
        except APIError:
            pass

    time.sleep(0.3)
    state = client.call("gamestate")
    # Re-read ox_locked after gamestate refresh
    ox_locked = state.get("round", {}).get("most_played_poker_hand")
    print(f"  Money: ${state.get('money', '?')}")

    hand_cards = state.get("hand", {}).get("cards", [])
    play_indices = list(range(len(hand_cards) - 5, len(hand_cards)))
    r = play_and_compare(client, state, play_indices,
                         f"Ox: playing Pair (locked={ox_locked})",
                         blind_name=blind_name,
                         ox_most_played=ox_locked)
    results.append(r)


def test_ox_different_hand(client, results):
    """Play a non-locked hand type — Bull should keep money."""
    print(f"\n{'='*60}")
    print("OX-B: Play different hand type — money kept")
    print(f"{'='*60}")

    state = setup_clean(client, "OX_B")
    state = advance_to_ox(client)
    if state is None:
        return

    ox_locked = state.get("round", {}).get("most_played_poker_hand")
    blind_name, _ = get_current_blind(state)
    print(f"  Blind: {blind_name}")
    print(f"  Ox locked hand: {ox_locked}")

    # Sell jokers, add Bull
    for _ in range(state.get("jokers", {}).get("count", 0)):
        try:
            client.call("sell", {"joker": 0})
        except APIError:
            pass
    try:
        client.call("add", {"key": "j_bull"})
    except APIError as e:
        print(f"  FAILED adding bull: {e.message}")

    # Set known money
    try:
        client.call("set", {"money": 200})
    except APIError:
        pass

    # Inject cards that DON'T make the locked type
    # Use a Straight (unlikely to be locked)
    state = client.call("gamestate")
    hc = state.get("hand", {}).get("cards", [])
    if hc:
        try:
            client.call("discard", {"cards": list(range(min(len(hc), 5)))})
            time.sleep(0.2)
        except APIError:
            pass
    for key in ["H_5", "D_6", "C_7", "S_8", "H_9"]:
        try:
            client.call("add", {"key": key})
        except APIError:
            pass

    time.sleep(0.3)
    state = client.call("gamestate")
    ox_locked = state.get("round", {}).get("most_played_poker_hand")
    print(f"  Money: ${state.get('money', '?')}")

    hand_cards = state.get("hand", {}).get("cards", [])
    play_indices = list(range(len(hand_cards) - 5, len(hand_cards)))
    r = play_and_compare(client, state, play_indices,
                         f"Ox: playing Straight (locked={ox_locked})",
                         blind_name=blind_name,
                         ox_most_played=ox_locked)
    results.append(r)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Integration tests for The Ox + Bull")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--start-server", action="store_true")
    parser.add_argument("--test", type=str, default="all",
                        help="Run specific test: ox_a, ox_b, or 'all'")
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
        "ox_a": ("Ox: play locked hand (money zeroed)", test_ox_locked_hand),
        "ox_b": ("Ox: play different hand (money kept)", test_ox_different_hand),
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

    if server_proc:
        stop_server(server_proc)


if __name__ == "__main__":
    main()
