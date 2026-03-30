"""Test win detection by injecting a god-mode build and playing to ante 9.

Usage:
    python test_win.py [--port PORT]

Requires a running balatrobot server (e.g. `uvx balatrobot serve --port 12346`).
Starts a game, injects overpowered jokers + money, sets ante to 7,
then lets the bot play through the ante 8 boss to verify win detection.
"""

import argparse
import logging
import sys
import time

sys.path.insert(0, "src")

from balatrobot.cli.client import BalatroClient, APIError
from balatro_bot.engine import RuleEngine
from balatro_bot.bot import run_bot, setup_logging


def inject_god_mode(client: BalatroClient) -> None:
    """Inject overpowered jokers and money into the current game."""
    # Wait for a state where we can inject (SELECTING_HAND or SHOP)
    for _ in range(20):
        state = client.call("gamestate")
        gs = state.get("state", "")
        if gs in ("SELECTING_HAND", "SHOP", "ROUND_EVAL"):
            break
        if gs == "BLIND_SELECT":
            client.call("select")
            time.sleep(0.3)
            continue
        time.sleep(0.3)
    else:
        print(f"ERROR: stuck in state {state.get('state')}, can't inject")
        return

    print(f"Injecting in state: {gs}")

    # Set money high
    try:
        client.call("set", {"money": 999})
        print("  Set money to 999")
    except APIError as e:
        print(f"  set money failed: {e.message}")

    # Set ante to 8 (just need to beat the ante 8 boss blind to win)
    try:
        client.call("set", {"ante": 8})
        print("  Set ante to 8")
    except APIError as e:
        print(f"  set ante failed: {e.message}")

    # Level up High Card massively
    leveled = 0
    for i in range(50):
        try:
            client.call("add", {"key": "c_pluto"})
            client.call("use", {"consumable": 0})
            leveled += 1
        except APIError as e:
            print(f"  Planet loop stopped at {i}: {e.message}")
            break
        if i % 10 == 9:
            print(f"  ... leveled {i+1} times")
    state = client.call("gamestate")
    hc_level = state.get("hands", {}).get("High Card", {}).get("level", 1)
    print(f"  High Card leveled to {hc_level} ({leveled} planets used)")

    # Omega face card build — immediate power, no scaling needed
    god_jokers = [
        "j_photograph",     # X2 Mult on first face card scored
        "j_triboulet",      # X2 Mult per K or Q scored
        "j_smiley",         # +5 Mult per face card scored
        "j_scary_face",     # +30 Chips per face card scored
        "j_pareidolia",     # All cards count as face cards
    ]

    for jk in god_jokers:
        try:
            client.call("add", {"key": jk})
            print(f"  Added {jk}")
        except APIError as e:
            print(f"  add {jk} failed: {e.message}")

    state = client.call("gamestate")
    jokers = [j.get("label", "?") for j in state.get("jokers", {}).get("cards", [])]
    print(f"\nRoster: {jokers}")
    print(f"Money: ${state.get('money', 0)}")
    print(f"Ante: {state.get('ante_num', '?')}")
    print()


def main():
    parser = argparse.ArgumentParser(description="Test win detection with god-mode injection")
    parser.add_argument("--port", type=int, default=12346)
    args = parser.parse_args()

    # Set up logging to console
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("balatro_bot")
    log.setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    # Also set up scoring log to console so we can see it
    scoring_log = logging.getLogger("balatro_scoring")
    scoring_log.setLevel(logging.INFO)
    if not scoring_log.handlers:
        sh = logging.StreamHandler()
        sh.setFormatter(logging.Formatter("%(asctime)s [SCORE] %(message)s", datefmt="%H:%M:%S"))
        scoring_log.addHandler(sh)

    client = BalatroClient(port=args.port)

    # Health check
    try:
        client.call("health")
    except Exception as e:
        print(f"No server on port {args.port}: {e}")
        sys.exit(1)

    # Start a fresh game
    print("Starting new game...")
    try:
        client.call("menu")
    except APIError:
        pass
    time.sleep(0.5)

    state = client.call("start", {"deck": "RED", "stake": "WHITE", "seed": "GODMODE1"})
    print(f"Game started: seed={state.get('seed')} state={state.get('state')}")

    # Inject god mode
    inject_god_mode(client)

    # Run the bot
    print("=" * 60)
    print("Running bot — expecting VICTORY when ante 9 is reached")
    print("=" * 60)

    engine = RuleEngine()
    won = run_bot(client, engine, start_game=False, poll_interval=0.15)

    print()
    print("=" * 60)
    if won:
        print("TEST PASSED: Bot detected VICTORY")
    else:
        state = client.call("gamestate")
        print(f"TEST FAILED: Bot returned won=False")
        print(f"  Final state: {state.get('state')}")
        print(f"  Final ante: {state.get('ante_num')}")
        print(f"  state.won: {state.get('won')}")
    print("=" * 60)


if __name__ == "__main__":
    main()
