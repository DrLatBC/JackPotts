"""Integration tests for play order with Hanging Chad.

Verifies that the bot rearranges hand cards before playing so that
cards with per-card joker bonuses (Even Steven, Odd Todd, Arrowhead, etc.)
land in the first position for Hanging Chad's 3x retriggers.

Uses engine.decide() to get the bot's actual play action, then mirrors
bot.py's rearrange logic before playing in-game.

Usage:
    python test_play_order.py --start-server
    python test_play_order.py --port 12346
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

from harness import (
    TEST_PORT,
    wait_for_state, setup_game_full,
    ensure_server, stop_server,
)


# ── helpers ──────────────────────────────────────────────────────

def fmt_card(c):
    r = c.get("value", {}).get("rank", "?")
    s = c.get("value", {}).get("suit", "?")
    return f"{r}{s}"


EVEN_RANKS = {"2", "4", "6", "8", "T"}
ODD_RANKS = {"A", "3", "5", "7", "9"}


def bot_play(client, engine, state):
    """Run engine.decide(), apply bot.py's rearrange logic, play in-game.

    Returns dict with play details, or None on failure.
    """
    action = engine.decide(state)
    if action is None:
        return None

    method, params = action.to_rpc()
    if method != "play" or not params or "cards" not in params:
        return {"method": method, "skipped": True}

    play_indices = list(params["cards"])
    rearranged = False

    # Mirror bot.py rearrange logic
    if play_indices != sorted(play_indices):
        hand_size = len(state.get("hand", {}).get("cards", []))
        non_play = [i for i in range(hand_size) if i not in set(play_indices)]
        new_order = play_indices + non_play
        try:
            client.call("rearrange", {"hand": new_order})
            params["cards"] = list(range(len(play_indices)))
            state = client.call("gamestate")
            rearranged = True
        except APIError as e:
            print(f"    rearrange failed: {e.message}")

    hand_cards = state.get("hand", {}).get("cards", [])
    played_labels = [fmt_card(hand_cards[i]) for i in params["cards"] if i < len(hand_cards)]
    played_cards = [hand_cards[i] for i in params["cards"] if i < len(hand_cards)]

    pre_chips = state.get("round", {}).get("chips", 0)

    try:
        new_state = client.call("play", {"cards": params["cards"]})
    except APIError as e:
        print(f"    play failed: {e.message}")
        return None

    post_chips = new_state.get("round", {}).get("chips", 0)
    actual = post_chips - pre_chips

    return {
        "method": method,
        "original_indices": play_indices,
        "rearranged": rearranged,
        "played_labels": played_labels,
        "played_cards": played_cards,
        "first_card": played_cards[0] if played_cards else {},
        "actual_score": actual,
        "reason": getattr(action, "reason", ""),
    }


# ── tests ────────────────────────────────────────────────────────

def test_even_steven(client, engine):
    """Hanging Chad + Even Steven: even card should be placed first."""
    print("\n--- Test 1: Hanging Chad + Even Steven ---")

    state = setup_game_full(client, "HCORD1",
        joker_keys=["j_hanging_chad", "j_even_steven"],
        card_configs=[
            {"key": "H_9"}, {"key": "C_7"}, {"key": "S_8"},
            {"key": "D_3"}, {"key": "H_4"},
        ])

    hand_cards = state.get("hand", {}).get("cards", [])
    jokers = state.get("jokers", {}).get("cards", [])
    print(f"    Hand:   {[fmt_card(c) for c in hand_cards]}")
    print(f"    Jokers: {[j.get('key') for j in jokers]}")

    result = bot_play(client, engine, state)
    if result is None or result.get("skipped"):
        print(f"    SKIP: bot didn't play (method={result.get('method') if result else 'None'})")
        return {"name": "even_steven", "status": "SKIP"}

    print(f"    Played:     {result['played_labels']}")
    print(f"    First card: {fmt_card(result['first_card'])}")
    print(f"    Rearranged: {result['rearranged']}")
    print(f"    Score:      {result['actual_score']}")

    first_rank = result["first_card"].get("value", {}).get("rank", "?")
    if first_rank not in EVEN_RANKS:
        print(f"    >>> FAIL: first card rank '{first_rank}' is odd — should be even")
        return {"name": "even_steven", "status": "FAIL"}

    print(f"    OK: even card first ({first_rank})")
    return {"name": "even_steven", "status": "PASS"}


def test_odd_todd(client, engine):
    """Hanging Chad + Odd Todd: odd card should be placed first."""
    print("\n--- Test 2: Hanging Chad + Odd Todd ---")

    state = setup_game_full(client, "HCORD2",
        joker_keys=["j_hanging_chad", "j_odd_todd"],
        card_configs=[
            {"key": "D_T"}, {"key": "H_6"}, {"key": "S_9"},
        ])

    hand_cards = state.get("hand", {}).get("cards", [])
    jokers = state.get("jokers", {}).get("cards", [])
    print(f"    Hand:   {[fmt_card(c) for c in hand_cards]}")
    print(f"    Jokers: {[j.get('key') for j in jokers]}")

    result = bot_play(client, engine, state)
    if result is None or result.get("skipped"):
        print(f"    SKIP: bot didn't play")
        return {"name": "odd_todd", "status": "SKIP"}

    print(f"    Played:     {result['played_labels']}")
    print(f"    First card: {fmt_card(result['first_card'])}")
    print(f"    Rearranged: {result['rearranged']}")
    print(f"    Score:      {result['actual_score']}")

    first_rank = result["first_card"].get("value", {}).get("rank", "?")
    if first_rank not in ODD_RANKS:
        print(f"    >>> FAIL: first card rank '{first_rank}' is even — should be odd")
        return {"name": "odd_todd", "status": "FAIL"}

    print(f"    OK: odd card first ({first_rank})")
    return {"name": "odd_todd", "status": "PASS"}


def test_arrowhead(client, engine):
    """Hanging Chad + Arrowhead: spade should be placed first."""
    print("\n--- Test 3: Hanging Chad + Arrowhead ---")

    state = setup_game_full(client, "HCORD3",
        joker_keys=["j_hanging_chad", "j_arrowhead"],
        card_configs=[{"key": "H_T"}, {"key": "S_T"}])

    hand_cards = state.get("hand", {}).get("cards", [])
    jokers = state.get("jokers", {}).get("cards", [])
    print(f"    Hand:   {[fmt_card(c) for c in hand_cards]}")
    print(f"    Jokers: {[j.get('key') for j in jokers]}")

    result = bot_play(client, engine, state)
    if result is None or result.get("skipped"):
        print(f"    SKIP: bot didn't play")
        return {"name": "arrowhead", "status": "SKIP"}

    print(f"    Played:     {result['played_labels']}")
    print(f"    First card: {fmt_card(result['first_card'])}")
    print(f"    Rearranged: {result['rearranged']}")
    print(f"    Score:      {result['actual_score']}")

    first_suit = result["first_card"].get("value", {}).get("suit", "?")
    if first_suit != "S":
        print(f"    >>> FAIL: first card suit '{first_suit}' — should be S (spade)")
        return {"name": "arrowhead", "status": "FAIL"}

    print(f"    OK: spade first")
    return {"name": "arrowhead", "status": "PASS"}


def test_rearrange_improves_score(client):
    """Same Pair, two orders — spade first should score higher with Wrathful.

    Both cards score (Pair), but only the spade gets Wrathful +3 mult.
    Hanging Chad gives the first scoring card 3 triggers:
      Spade first:  3*3 + 1*0 = +9 mult from Wrathful
      Heart first:  3*0 + 1*3 = +3 mult from Wrathful
    """
    print("\n--- Test 4: Rearranged order scores higher ---")

    # Play A: natural order — heart first (no Wrathful bonus on retrigger)
    state_a = setup_game_full(client, "HCORD4A",
        joker_keys=["j_hanging_chad", "j_wrathful_joker"],
        card_configs=[{"key": "H_T"}, {"key": "S_T"}])

    hand_a = state_a.get("hand", {}).get("cards", [])
    n = len(hand_a)
    play_a = [n - 2, n - 1]  # natural: H_T then S_T
    pre_a = state_a.get("round", {}).get("chips", 0)
    print(f"    A (natural):    {[fmt_card(hand_a[i]) for i in play_a]}")

    try:
        new_a = client.call("play", {"cards": play_a})
        score_a = new_a.get("round", {}).get("chips", 0) - pre_a
    except APIError as e:
        print(f"    Play A failed: {e.message}")
        return {"name": "rearrange_improves", "status": "FAIL"}
    print(f"    Score A (TH first): {score_a}")

    # Play B: rearranged — spade first (Wrathful +3 mult x3 triggers)
    state_b = setup_game_full(client, "HCORD4B",
        joker_keys=["j_hanging_chad", "j_wrathful_joker"],
        card_configs=[{"key": "H_T"}, {"key": "S_T"}])

    hand_b = state_b.get("hand", {}).get("cards", [])
    n = len(hand_b)
    idx_h = n - 2  # H_T
    idx_s = n - 1  # S_T
    non_play = [i for i in range(n) if i not in (idx_h, idx_s)]
    new_order = [idx_s, idx_h] + non_play  # spade first
    client.call("rearrange", {"hand": new_order})
    time.sleep(0.3)
    state_b = client.call("gamestate")

    hand_b2 = state_b.get("hand", {}).get("cards", [])
    print(f"    B (rearranged): {[fmt_card(hand_b2[i]) for i in [0, 1]]}")
    pre_b = state_b.get("round", {}).get("chips", 0)

    try:
        new_b = client.call("play", {"cards": [0, 1]})
        score_b = new_b.get("round", {}).get("chips", 0) - pre_b
    except APIError as e:
        print(f"    Play B failed: {e.message}")
        return {"name": "rearrange_improves", "status": "FAIL"}
    print(f"    Score B (TS first): {score_b}")

    diff = score_b - score_a
    print(f"    Improvement: {diff:+d}")

    if score_b > score_a:
        print(f"    OK: rearranged order scored higher (spade first = more Wrathful triggers)")
        return {"name": "rearrange_improves", "status": "PASS"}
    elif score_b == score_a:
        print(f"    >>> FAIL: scores equal — rearrange not affecting scoring order")
        return {"name": "rearrange_improves", "status": "FAIL"}
    else:
        print(f"    >>> FAIL: natural order scored higher — something is wrong")
        return {"name": "rearrange_improves", "status": "FAIL"}


# ── runner ───────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Integration tests for play order with Hanging Chad")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--start-server", action="store_true")
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

    engine = RuleEngine()
    results = []

    try:
        results.append(test_even_steven(client, engine))
        results.append(test_odd_todd(client, engine))
        results.append(test_arrowhead(client, engine))
        results.append(test_rearrange_improves_score(client))
    finally:
        if server_proc:
            stop_server(server_proc)

    print("\n" + "=" * 60)
    print("SUMMARY — Play Order (Hanging Chad)")
    print("=" * 60)

    passes = sum(1 for r in results if r and r["status"] == "PASS")
    fails = sum(1 for r in results if r and r["status"] == "FAIL")
    skips = sum(1 for r in results if r and r["status"] == "SKIP")

    for r in results:
        if r:
            icon = {"PASS": "OK", "FAIL": "XX", "SKIP": "--"}[r["status"]]
            print(f"  [{icon}] {r['name']}")

    print(f"\n  {passes} PASS, {fails} FAIL, {skips} SKIP out of {len(results)} tests")

    if fails > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
