"""Smoke test: verify every boss blind can be forced via the set API.

For each boss: start game, set ante to minimum, beat SB+BB, force boss, verify.

Usage:
    python test_force_boss.py [--port PORT] [--start-server]
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from balatrobot.cli.client import BalatroClient, APIError

from harness import (
    TEST_PORT, BOSS_KEYS, BOSS_MIN_ANTE,
    wait_for_state, setup_game, get_current_blind, get_boss_name,
    is_boss_blind_select, beat_blind_fast, set_ante,
    advance_through_post_blind, ensure_server, stop_server,
    take_screenshot,
)


def test_force_boss(client, boss_name, run_num):
    """Start game, set ante, beat SB+BB, force boss, verify. Returns True/False."""
    min_ante = BOSS_MIN_ANTE.get(boss_name, 1)
    boss_key = BOSS_KEYS.get(boss_name)

    print(f"\n  [{run_num}] {boss_name} (ante {min_ante}, {boss_key})")

    # Fresh game
    try:
        state = setup_game(client, f"FORCE_{run_num}")
    except (TimeoutError, APIError) as e:
        print(f"      FAIL: setup_game: {e}")
        return False

    set_ante(client, min_ante)
    try:
        client.call("set", {"money": 999999})
    except APIError:
        pass

    # Beat SB + BB
    for blind_label in ("Small Blind", "Big Blind"):
        for _ in range(30):
            state = client.call("gamestate")
            gs = state.get("state", "")
            if gs == "SELECTING_HAND":
                cur, _ = get_current_blind(state)
                if cur == blind_label:
                    beat_blind_fast(client, state)
                    break
                elif cur not in ("Small Blind", "Big Blind"):
                    break
            elif gs == "BLIND_SELECT":
                if is_boss_blind_select(state):
                    break
                client.call("select")
                time.sleep(0.3)
            elif gs == "ROUND_EVAL":
                try:
                    client.call("cash_out")
                except APIError:
                    pass
                time.sleep(0.3)
            elif gs == "SHOP":
                client.call("next_round")
                time.sleep(0.3)
            else:
                time.sleep(0.3)

    # Should be at BLIND_SELECT with boss on deck
    for _ in range(20):
        state = client.call("gamestate")
        if state.get("state") == "BLIND_SELECT" and is_boss_blind_select(state):
            break
        if state.get("state") == "ROUND_EVAL":
            try:
                client.call("cash_out")
            except APIError:
                pass
        elif state.get("state") == "SHOP":
            client.call("next_round")
        time.sleep(0.3)

    if state.get("state") != "BLIND_SELECT":
        print(f"      FAIL: not at BLIND_SELECT, stuck in {state.get('state')}")
        take_screenshot(client, f"force_fail_{boss_name}_not_blind_select")
        return False

    # Force the boss
    try:
        client.call("set", {"blind": boss_key})
    except APIError as e:
        print(f"      FAIL: set blind: {e.message}")
        return False

    time.sleep(0.3)
    state = client.call("gamestate")
    actual = get_boss_name(state)

    if actual == boss_name:
        print(f"      OK")
        return True
    else:
        print(f"      FAIL: expected '{boss_name}', got '{actual}'")
        take_screenshot(client, f"force_fail_{boss_name}_wrong_boss")
        return False


def main():
    parser = argparse.ArgumentParser(description="Smoke test: force every boss blind")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--start-server", action="store_true")
    parser.add_argument("--boss", type=str, default=None,
                        help="Test a single boss by name (default: all)")
    args = parser.parse_args()

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

    if args.boss:
        if args.boss not in BOSS_KEYS:
            close = [b for b in BOSS_KEYS if args.boss.lower() in b.lower()]
            print(f"Unknown boss '{args.boss}'." + (f" Did you mean: {', '.join(close)}?" if close else ""))
            sys.exit(1)
        bosses = [args.boss]
    else:
        bosses = sorted(BOSS_KEYS.keys(), key=lambda n: (BOSS_MIN_ANTE.get(n, 0), n))

    print(f"Testing {len(bosses)} boss blind(s)...")

    passed = []
    failed = []

    for i, name in enumerate(bosses, 1):
        ok = test_force_boss(client, name, i)
        (passed if ok else failed).append(name)

    # Report
    print(f"\n{'='*60}")
    print(f"RESULTS: {len(passed)} passed, {len(failed)} failed out of {len(bosses)}")
    print(f"{'='*60}")

    if failed:
        print(f"\nFailed:")
        for name in failed:
            print(f"  {name} ({BOSS_KEYS[name]}, min ante {BOSS_MIN_ANTE.get(name, '?')})")

    if server_proc:
        stop_server(server_proc)

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
