"""Integration test: bot survives the win screen without crashing.

Reproduces game_102 crash (2026-04-11):
  Bot beats ante 8 boss → cash_out triggers the win screen →
  mod becomes unresponsive → double timeout → crash.

Cheats to ante 8 boss select, sells jokers, burns hands down to 1,
then injects polychrome god-mode jokers so the bot one-shots the boss.
When the win screen fires, the bot should exit gracefully.

MUST run headed — the post-win timeout is caused by the Lua mod's
UI event handling after G.GAME.won fires.

Usage:
    python test_win_crash.py [--port PORT]
"""

import argparse
import logging
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "support"))

from balatrobot.cli.client import BalatroClient, APIError
from balatro_bot.engine import RuleEngine
from balatro_bot.bot import run_bot, setup_logging
from harness import (
    start_server, stop_server, setup_game, inject_jokers,
    advance_to_boss_select, get_boss_name, wait_for_state,
    TEST_PORT, _check_port,
)


def main():
    parser = argparse.ArgumentParser(
        description="Test: bot exits gracefully on win screen (no crash on double timeout)"
    )
    parser.add_argument("--port", type=int, default=TEST_PORT)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    # Must be headed — post-win timeout only happens with the real game UI
    server_proc = None
    if _check_port(args.port):
        print(f"  Server already running on port {args.port}")
        client = BalatroClient(port=args.port)
    else:
        server_proc = start_server(port=args.port, headless=False, fast=True)
        client = BalatroClient(port=args.port)

    try:
        _run_test(client)
    finally:
        if server_proc:
            stop_server(server_proc)


def _run_test(client: BalatroClient):
    print("\n" + "=" * 60)
    print("TEST: Bot survives the win screen")
    print("=" * 60)

    # Start fresh game, cheat to ante 8 boss select
    setup_game(client, seed="WINCRASH1")
    advance_to_boss_select(client, target_ante=8)

    state = client.call("gamestate")
    print(f"\n  Ante 8 boss: {get_boss_name(state)}")
    print(f"  State: {state.get('state')}")

    # Select the boss so we're in SELECTING_HAND
    if state.get("state") == "BLIND_SELECT":
        client.call("select")
        time.sleep(0.5)
        state = wait_for_state(client, {"SELECTING_HAND"})

    # Sell all existing jokers
    joker_count = state.get("jokers", {}).get("count", 0)
    for _ in range(joker_count):
        try:
            client.call("sell", {"joker": 0})
        except APIError:
            pass
    print(f"  Sold {joker_count} jokers")

    # Burn hands down to 1 by playing garbage
    state = client.call("gamestate")
    hands_left = state.get("round", {}).get("hands_left", 4)
    while hands_left > 1:
        hand_cards = state.get("hand", {}).get("cards", [])
        n = min(5, len(hand_cards))
        if n == 0:
            break
        try:
            client.call("play", {"cards": list(range(n))})
        except APIError:
            break
        time.sleep(0.3)
        state = client.call("gamestate")
        # Wait through transition states
        for _ in range(10):
            if state.get("state") == "SELECTING_HAND":
                break
            time.sleep(0.2)
            state = client.call("gamestate")
        hands_left = state.get("round", {}).get("hands_left", 0)
    print(f"  Hands remaining: {hands_left}")

    # Inject polychrome god-mode jokers
    poly_jokers = [
        "j_photograph", "j_triboulet", "j_smiley",
        "j_scary_face", "j_pareidolia",
    ]
    for jk in poly_jokers:
        try:
            client.call("add", {"key": jk, "edition": "POLYCHROME"})
            print(f"    Added {jk} (Polychrome)")
        except APIError as e:
            print(f"    FAILED adding {jk}: {e.message}")

    # Level up High Card massively so any 5-card hand one-shots the boss
    leveled = 0
    for _ in range(50):
        try:
            client.call("add", {"key": "c_pluto"})
            client.call("use", {"consumable": 0})
            leveled += 1
        except APIError:
            break
    print(f"    Leveled High Card {leveled} times")

    state = client.call("gamestate")
    jokers = [j.get("label", "?") for j in state.get("jokers", {}).get("cards", [])]
    print(f"\n  Roster: {jokers}")
    print(f"  Hands left: {state.get('round', {}).get('hands_left')}")

    # Let the bot play — one hand to beat the boss, then win screen fires
    print("\n  Running bot — expecting win screen survival...")
    engine = RuleEngine()

    try:
        won = run_bot(client, engine, start_game=False, poll_interval=0.15)
    except Exception as e:
        print(f"\n  FAIL: Bot crashed with {type(e).__name__}: {e}")
        print("=" * 60)
        sys.exit(1)

    print()
    print("=" * 60)
    if won:
        print("PASS: Bot returned won=True (survived win screen)")
    else:
        print("PARTIAL: Bot didn't crash, but returned won=False")
        print("  (victory detection may need further work)")
    print("=" * 60)

    if not won:
        sys.exit(2)


if __name__ == "__main__":
    main()
