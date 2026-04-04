"""Test harness for specific boss blinds.

Usage:
    python test_boss.py "The Hook" --ante 3 [--port PORT] [--seed SEED]
    python test_boss.py "The Eye" --ante 5 --god-mode
    python test_boss.py --list

Fast-forwards to the target ante by setting chips to instantly beat blinds,
then forces the desired boss via the set API. Then runs the bot against it.

Requires a running balatrobot server (e.g. `uvx balatrobot serve --port 12346`).
"""

import argparse
import logging
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from balatrobot.cli.client import BalatroClient, APIError
from balatro_bot.engine import RuleEngine
from balatro_bot.bot import run_bot
from balatro_bot.domain.policy.playing import BOSS_BLINDS

from harness import (
    TEST_PORT, BOSS_KEYS, BOSS_MIN_ANTE,
    wait_for_state, get_boss_name, get_current_blind,
    advance_to_boss_select, force_boss, inject_god_mode,
    ensure_server, stop_server,
)


def main():
    parser = argparse.ArgumentParser(
        description="Test bot against a specific boss blind",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Available bosses:\n  " + "\n  ".join(sorted(BOSS_BLINDS)),
    )
    parser.add_argument("boss", nargs="?", help="Boss blind name (e.g. 'The Hook')")
    parser.add_argument("--ante", type=int, default=None,
                        help="Ante to test at (default: boss's minimum ante)")
    parser.add_argument("--port", type=int, default=None,
                        help=f"Server port (default: {TEST_PORT} with --start-server, 12346 without)")
    parser.add_argument("--seed", type=str, default=None,
                        help="Game seed (random if omitted)")
    parser.add_argument("--god-mode", action="store_true",
                        help="Inject overpowered build to guarantee beating the boss")
    parser.add_argument("--start-server", action="store_true",
                        help="Auto-start a balatrobot server (killed on exit)")
    parser.add_argument("--list", action="store_true",
                        help="List all boss blinds and exit")
    args = parser.parse_args()

    if args.list:
        print("Boss blinds:")
        for name in sorted(BOSS_BLINDS):
            key = BOSS_KEYS.get(name, "?")
            min_ante = BOSS_MIN_ANTE.get(name, "?")
            print(f"  {name:20s}  ({key:20s})  min ante: {min_ante}")
        sys.exit(0)

    if not args.boss:
        parser.error("boss name is required (use --list to see options)")

    if args.boss not in BOSS_BLINDS:
        close = [b for b in BOSS_BLINDS if args.boss.lower() in b.lower()]
        if close:
            print(f"Unknown boss '{args.boss}'. Did you mean: {', '.join(close)}?")
        else:
            print(f"Unknown boss '{args.boss}'. Use --list to see available bosses.")
        sys.exit(1)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    scoring_log = logging.getLogger("balatro_scoring")
    scoring_log.setLevel(logging.INFO)
    if not scoring_log.handlers:
        sh = logging.StreamHandler()
        sh.setFormatter(logging.Formatter("%(asctime)s [SCORE] %(message)s", datefmt="%H:%M:%S"))
        scoring_log.addHandler(sh)

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

    # Start fresh game
    import random, string
    seed = args.seed or "".join(random.choices(string.ascii_uppercase + string.digits, k=8))
    print(f"Starting game with seed={seed}...")
    try:
        client.call("menu")
    except APIError:
        pass
    time.sleep(0.5)

    state = client.call("start", {"deck": "RED", "stake": "WHITE", "seed": seed})
    print(f"Game started: seed={state.get('seed')} state={state.get('state')}")

    # Get into SELECTING_HAND so we can use set commands
    print(f"\n--- Getting into playable state ---")
    state = wait_for_state(client, {"SELECTING_HAND"})
    print(f"  In state: {state.get('state')}")

    # Resolve ante: explicit flag > boss minimum > fallback 1
    ante = args.ante or BOSS_MIN_ANTE.get(args.boss, 1)

    # Fast-forward to boss blind select at target ante
    print(f"\n--- Advancing to ante {ante} boss ---")
    state = advance_to_boss_select(client, ante)
    boss = get_boss_name(state)
    print(f"  At boss blind select: {boss}")

    # Force the target boss
    if boss != args.boss:
        print(f"\n--- Forcing boss: {args.boss} ---")
        found = force_boss(client, args.boss)
        if not found:
            state = client.call("gamestate")
            actual = get_boss_name(state)
            print(f"\nCould not force {args.boss}. Current boss: {actual}")
            resp = input("Continue anyway? [y/N] ")
            if resp.lower() != "y":
                sys.exit(0)

    # God mode if requested
    if args.god_mode:
        client.call("select")
        time.sleep(0.3)
        state = wait_for_state(client, {"SELECTING_HAND"})
        print(f"\n--- Injecting god-mode build ---")
        inject_god_mode(client)

    # Select boss blind and run bot
    state = client.call("gamestate")
    gs = state.get("state", "")
    if gs == "BLIND_SELECT":
        client.call("select")
        time.sleep(0.3)

    state = client.call("gamestate")
    blind_name, _ = get_current_blind(state)
    print(f"\n{'='*60}")
    print(f"TESTING: {blind_name} at ante {state.get('ante_num')}")
    print(f"{'='*60}\n")

    engine = RuleEngine()
    won = run_bot(client, engine, start_game=False, poll_interval=0.15)

    print(f"\n{'='*60}")
    state = client.call("gamestate")
    if state.get("state") == "GAME_OVER":
        print(f"RESULT: Lost against {blind_name}")
    elif won:
        print(f"RESULT: Beat {blind_name} and won the run!")
    else:
        print(f"RESULT: Beat {blind_name}, run continued to ante {state.get('ante_num')}")
    print(f"{'='*60}")

    if server_proc:
        stop_server(server_proc)


if __name__ == "__main__":
    main()
