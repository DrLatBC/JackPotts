"""Integration test: verifies the cash_out GAME_OVER fix.

Same multi-game scenario as test_win_multigame_bug.py, but patches
cash_out.lua BEFORE starting the server so that:
  1. BB_GAMESTATE.on_game_over = send_response is set before cash_out
  2. The inner condition event also checks for GAME_OVER (not just SHOP)

This mirrors what play.lua already does — the fix is adding the same
GAME_OVER handling to cash_out.lua.

MUST run headed — the win-screen hang only triggers with the real UI.

Usage:
    python test_win_multigame_fix.py [--port PORT]
"""

import argparse
import logging
import os
import re
import shutil
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "support"))

from balatrobot.cli.client import BalatroClient, APIError
from balatro_bot.engine import RuleEngine
from balatro_bot.bot import run_bot, setup_logging
from balatro_bot.config import SupervisorConfig
from harness import (
    start_server, stop_server, setup_game, inject_god_mode_complex,
    advance_to_boss_select, get_boss_name, wait_for_state,
    TEST_PORT, _check_port,
)

_cfg = SupervisorConfig()

# Path to the installed cash_out.lua
CASH_OUT_LUA = os.path.join(
    os.environ.get("APPDATA", ""),
    "Balatro", "Mods", "balatrobot-1.4.1",
    "src", "lua", "endpoints", "cash_out.lua",
)


def patch_cash_out():
    """Patch cash_out.lua to handle GAME_OVER (like play.lua does).

    Returns the original content for restore, or None if patch not needed.
    """
    if not os.path.exists(CASH_OUT_LUA):
        print(f"  WARNING: {CASH_OUT_LUA} not found, skipping patch")
        return None

    with open(CASH_OUT_LUA, "r") as f:
        original = f.read()

    if "on_game_over" in original:
        print("  cash_out.lua already has on_game_over handling, skipping patch")
        return None

    # Patch 1: Add on_game_over callback before G.FUNCS.cash_out call
    patched = original.replace(
        '        sendDebugMessage("cash_out() - scoring complete, triggering cash out", "BB.ENDPOINTS")\n'
        '        G.FUNCS.cash_out({ config = {} })',

        '        sendDebugMessage("cash_out() - scoring complete, triggering cash out", "BB.ENDPOINTS")\n'
        '        -- [FIX] Handle GAME_OVER transition (won state in endless mode)\n'
        '        BB_GAMESTATE.on_game_over = send_response\n'
        '        G.FUNCS.cash_out({ config = {} })',
    )

    # Patch 2: Add GAME_OVER check in inner condition event
    patched = patched.replace(
        '            local done = false\n'
        '            if G.STATE == G.STATES.SHOP and G.STATE_COMPLETE then',

        '            local done = false\n'
        '            -- [FIX] If game ended (won state), deliver response via this check\n'
        '            if G.STATE == G.STATES.GAME_OVER then\n'
        '              send_response(BB_GAMESTATE.get_gamestate())\n'
        '              return true\n'
        '            end\n'
        '            if G.STATE == G.STATES.SHOP and G.STATE_COMPLETE then',
    )

    if patched == original:
        print("  WARNING: Patch markers not found in cash_out.lua — file may have changed")
        return None

    with open(CASH_OUT_LUA, "w") as f:
        f.write(patched)

    print("  Patched cash_out.lua with GAME_OVER handling")
    return original


def restore_cash_out(original_content):
    """Restore cash_out.lua to its original content."""
    if original_content is None:
        return
    with open(CASH_OUT_LUA, "w") as f:
        f.write(original_content)
    print("  Restored original cash_out.lua")


def main():
    parser = argparse.ArgumentParser(
        description="Test: cash_out GAME_OVER fix — server survives win screen"
    )
    parser.add_argument("--port", type=int, default=TEST_PORT)
    args = parser.parse_args()

    # Match stream mode logging exactly — setup_logging with stream_file
    log_dir = os.path.join(os.path.dirname(__file__), "..", "..", "logs", "test_multigame_fix")
    os.makedirs(log_dir, exist_ok=True)
    setup_logging(
        log_file=os.path.join(log_dir, "game.log"),
        wins_file=os.path.join(log_dir, "wins.txt"),
        scoring_file=os.path.join(log_dir, "scoring.log"),
        stream_file=os.path.join(log_dir, "stream.log"),
    )

    # Patch cash_out.lua BEFORE starting the server
    original_lua = patch_cash_out()

    server_proc = None
    try:
        if _check_port(args.port):
            print(f"  Server already running on port {args.port}")
            print("  WARNING: server was started before patch — results may not reflect fix")
            client = BalatroClient(port=args.port)
        else:
            # fast=False to match stream mode — must reproduce the same conditions
            server_proc = start_server(port=args.port, headless=False, fast=False)
            client = BalatroClient(port=args.port)

        _run_test(client)
    finally:
        if server_proc:
            stop_server(server_proc)
        restore_cash_out(original_lua)


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
    print("TEST: cash_out GAME_OVER fix — multi-game win survival")
    print("=" * 60)

    # ---- Game 1: win at ante 8 ----
    print("\n--- GAME 1: Win at ante 8 ---")
    _setup_god_mode_ante8(client, seed="FIXTEST1")

    engine = RuleEngine()
    try:
        won = run_bot(client, engine, start_game=False, poll_interval=0.2,
                      stream_delay=2.0)
    except Exception as e:
        print(f"\n  FAIL (game 1): Bot crashed with {type(e).__name__}: {e}")
        sys.exit(1)

    # run_bot may return False even on a real win — see test_win_multigame_bug.py
    print(f"\n  Game 1 result: won={won} (victory detection may lag — OK)")

    # ---- Check server health after win ----
    print("\n--- POST-WIN: Checking server health ---")
    time.sleep(1)

    try:
        health = client.call("health")
        print(f"  Server health: OK")
    except Exception as e:
        print(f"\n  FAIL: Server dead after win even WITH fix — {type(e).__name__}: {e}")
        print("  The GAME_OVER handling patch didn't prevent the crash.")
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
            "deck": "RED", "stake": "WHITE", "seed": "FIXTEST2",
        })
        print(f"  Game 2 started: state={state.get('state')}")
    except Exception as e:
        print(f"\n  FAIL (game 2): Could not start new game — {type(e).__name__}: {e}")
        sys.exit(1)

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
    print("PASS: Fix works — server survived win screen, game 2 started")
    print("=" * 60)


if __name__ == "__main__":
    main()
