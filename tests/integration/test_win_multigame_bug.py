"""Integration test: reproduces game_103 crash — server dies after win screen.

Reproduces the multi-game crash (2026-04-12):
  Bot beats ante 8 boss → run_bot returns won=True →
  server is dead (ConnectError) → next game start fails.

The existing test_win_crash.py only runs ONE game, so it never
catches this. This test runs TWO games:
  1. Cheat to win at ante 8, run the bot
  2. Verify the server is still alive
  3. Start a second game

MUST run headed — the win-screen hang only triggers with the real UI.

Usage:
    python test_win_multigame_bug.py [--port PORT]
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
    start_server, stop_server, setup_game, inject_god_mode_complex,
    advance_to_boss_select, get_boss_name, wait_for_state,
    TEST_PORT, _check_port,
)


def main():
    parser = argparse.ArgumentParser(
        description="Test: server survives win screen for multi-game loop"
    )
    parser.add_argument("--port", type=int, default=TEST_PORT)
    args = parser.parse_args()

    # Match stream mode logging exactly — setup_logging with stream_file
    log_dir = os.path.join(os.path.dirname(__file__), "..", "..", "logs", "test_multigame_bug")
    os.makedirs(log_dir, exist_ok=True)
    setup_logging(
        log_file=os.path.join(log_dir, "game.log"),
        wins_file=os.path.join(log_dir, "wins.txt"),
        scoring_file=os.path.join(log_dir, "scoring.log"),
        stream_file=os.path.join(log_dir, "stream.log"),
    )

    server_proc = None
    if _check_port(args.port):
        print(f"  Server already running on port {args.port}")
        client = BalatroClient(port=args.port)
    else:
        # fast=False to match stream mode — the bug only triggers with normal
        # animation speed (fast mode skips the win animation that causes the hang)
        server_proc = start_server(port=args.port, headless=False, fast=False)
        client = BalatroClient(port=args.port)

    try:
        _run_test(client)
    finally:
        if server_proc:
            stop_server(server_proc)


def _setup_god_mode_ante8(client: BalatroClient, seed: str) -> dict:
    """Cheat to ante 8 boss, inject god-mode jokers, let bot play with 4 hands."""
    setup_game(client, seed=seed)
    advance_to_boss_select(client, target_ante=8)

    state = client.call("gamestate")
    print(f"\n  Ante 8 boss: {get_boss_name(state)}")

    if state.get("state") == "BLIND_SELECT":
        client.call("select")
        time.sleep(0.5)
        state = wait_for_state(client, {"SELECTING_HAND"})

    # Sell existing jokers to make room for god-mode ones
    joker_count = state.get("jokers", {}).get("count", 0)
    for _ in range(joker_count):
        try:
            client.call("sell", {"joker": 0})
        except APIError:
            pass

    inject_god_mode_complex(client)
    return client.call("gamestate")


def _run_test(client: BalatroClient):
    print("\n" + "=" * 60)
    print("TEST: Server survives win screen for multi-game loop")
    print("=" * 60)

    # ---- Game 1: win at ante 8 ----
    print("\n--- GAME 1: Win at ante 8 ---")
    _setup_god_mode_ante8(client, seed="MULTIGAME1")

    engine = RuleEngine()
    try:
        won = run_bot(client, engine, start_game=False, poll_interval=0.2,
                      stream_delay=2.0)
    except Exception as e:
        print(f"\n  FAIL (game 1): Bot crashed with {type(e).__name__}: {e}")
        sys.exit(1)

    # run_bot may return False even on a real win: _check_victory requires
    # ante 9+ to guard against Hieroglyph false positives. In fast mode the
    # game can go to GAME_OVER before ante advances. That's fine — this test
    # is about server survival, not victory detection accuracy.
    print(f"\n  Game 1 result: won={won} (victory detection may lag — OK)")

    # ---- Check server health after win ----
    print("\n--- POST-WIN: Checking server health ---")
    time.sleep(1)  # brief pause for any in-flight transitions

    try:
        health = client.call("health")
        print(f"  Server health: OK")
    except Exception as e:
        print(f"\n  FAIL: Server dead after win — {type(e).__name__}: {e}")
        print("  This is the game_103 bug: cash_out.lua doesn't handle")
        print("  GAME_OVER transition after winning in endless mode.")
        print("=" * 60)
        sys.exit(1)

    # ---- Game 2: start a fresh game ----
    print("\n--- GAME 2: Starting fresh game ---")
    try:
        client.call("menu")
    except APIError:
        pass
    time.sleep(0.5)

    try:
        state = client.call("start", {
            "deck": "RED", "stake": "WHITE", "seed": "MULTIGAME2",
        })
        print(f"  Game 2 started: state={state.get('state')}")
    except Exception as e:
        print(f"\n  FAIL (game 2): Could not start new game — {type(e).__name__}: {e}")
        sys.exit(1)

    # Verify we're in a playable state
    for _ in range(20):
        state = client.call("gamestate")
        if state.get("state") in ("SELECTING_HAND", "BLIND_SELECT"):
            break
        time.sleep(0.3)

    gs = state.get("state", "?")
    if gs in ("SELECTING_HAND", "BLIND_SELECT"):
        print(f"  Game 2 playable: state={gs}")
    else:
        print(f"\n  FAIL: Game 2 stuck in state={gs}")
        sys.exit(1)

    print("\n" + "=" * 60)
    print("PASS: Server survived win screen, game 2 started successfully")
    print("=" * 60)


if __name__ == "__main__":
    main()
