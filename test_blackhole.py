"""Test Black Hole pack pick doesn't soft-lock.

Uses seed U6IIG428 which reliably hits Black Hole in a Celestial Pack.
Runs the bot and checks that the game progresses past the pack pick
without getting stuck in a timeout loop.

Usage:
    python test_blackhole.py [--port PORT]

Requires a running balatrobot server.
"""

import argparse
import logging
import sys
import threading
import time

sys.path.insert(0, "src")

from balatrobot.cli.client import BalatroClient, APIError
from balatro_bot.engine import RuleEngine
from balatro_bot.bot import run_bot


STUCK_SEED = "U6IIG428"
# Black Hole appeared around action #78 in the original logs, at ante 3-ish.
# The bot should get past that point within a few minutes.
TIMEOUT_SECONDS = 180  # 3 minutes — plenty of time if not stuck


def main():
    parser = argparse.ArgumentParser(description="Test Black Hole pack pick")
    parser.add_argument("--port", type=int, default=12346)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("balatro_bot")
    log.setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    client = BalatroClient(port=args.port)

    try:
        client.call("health")
    except Exception as e:
        print(f"No server on port {args.port}: {e}")
        sys.exit(1)

    # Start game with the known stuck seed
    print(f"Starting game with seed {STUCK_SEED}...")
    try:
        client.call("menu")
    except APIError:
        pass
    time.sleep(0.5)

    state = client.call("start", {"deck": "RED", "stake": "WHITE", "seed": STUCK_SEED})
    print(f"Game started: seed={state.get('seed')} state={state.get('state')}")

    # Run the bot in a thread with a timeout
    result = {"won": None, "error": None, "finished": False}

    def run():
        try:
            engine = RuleEngine()
            result["won"] = run_bot(client, engine, start_game=False, poll_interval=0.15)
            result["finished"] = True
        except Exception as e:
            result["error"] = str(e)
            result["finished"] = True

    t = threading.Thread(target=run, daemon=True)
    print(f"Running bot (timeout={TIMEOUT_SECONDS}s)...")
    print("Watching for Black Hole picks...")
    print("=" * 60)
    t.start()
    t.join(timeout=TIMEOUT_SECONDS)

    print()
    print("=" * 60)
    if not result["finished"]:
        # Bot is still running after timeout — likely stuck
        # Check current state
        try:
            state = client.call("gamestate")
            gs = state.get("state", "?")
            ante = state.get("ante_num", "?")
            pack_cards = state.get("pack", {}).get("cards", [])
            pack_labels = [c.get("label", "?") for c in pack_cards]
        except Exception:
            gs, ante, pack_labels = "?", "?", []

        if gs == "SMODS_BOOSTER_OPENED" and any("Black Hole" in l for l in pack_labels):
            print(f"TEST FAILED: Bot stuck on Black Hole pack pick!")
            print(f"  State: {gs}, Ante: {ante}")
            print(f"  Pack cards: {pack_labels}")
        else:
            print(f"TEST INCONCLUSIVE: Bot timed out but not on Black Hole")
            print(f"  State: {gs}, Ante: {ante}")
        sys.exit(1)
    elif result["error"]:
        print(f"TEST ERROR: {result['error']}")
        sys.exit(1)
    else:
        print(f"TEST PASSED: Bot completed (won={result['won']})")
        print("Black Hole pack pick did not cause a soft-lock.")
    print("=" * 60)


if __name__ == "__main__":
    main()
