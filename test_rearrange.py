"""Integration tests for card rearranging (hand and jokers).

Verifies that the rearrange API actually reorders cards and the new
order persists across gamestate polls (i.e., align_cards() doesn't
undo it on the next frame).

Usage:
    python test_rearrange.py [--port PORT]
"""

import argparse
import sys
import time

sys.path.insert(0, "src")

from balatrobot.cli.client import BalatroClient, APIError


# ── helpers ──────────────────────────────────────────────────────

def give_money(client, amount=9999):
    try:
        client.call("set", {"money": amount})
    except APIError:
        pass


def wait_for_state(client, target_states, max_tries=30):
    for _ in range(max_tries):
        state = client.call("gamestate")
        gs = state.get("state", "")
        if gs in target_states:
            return state
        if gs == "BLIND_SELECT":
            client.call("select")
            time.sleep(0.3)
        elif gs in ("HAND_PLAYED", "DRAW_TO_HAND", "NEW_ROUND", "ROUND_EVAL"):
            time.sleep(0.3)
        elif gs == "SHOP":
            client.call("next_round")
            time.sleep(0.3)
        else:
            time.sleep(0.3)
    raise TimeoutError(f"Never reached {target_states}, stuck in {state.get('state')}")


def advance_to_shop(client, max_tries=40):
    """From any state, advance until we reach SHOP."""
    for _ in range(max_tries):
        state = client.call("gamestate")
        gs = state.get("state", "")
        if gs == "SHOP":
            return state
        if gs == "SELECTING_HAND":
            # Beat the blind quickly
            try:
                client.call("set", {"chips": 999999})
            except APIError:
                pass
            hc = state.get("hand", {}).get("cards", [])
            if hc:
                try:
                    client.call("play", {"cards": list(range(min(5, len(hc))))})
                except APIError:
                    pass
            time.sleep(0.5)
        elif gs == "ROUND_EVAL":
            try:
                client.call("cash_out")
            except APIError:
                pass
            time.sleep(0.3)
        elif gs == "BLIND_SELECT":
            try:
                client.call("select")
            except APIError:
                pass
            time.sleep(0.3)
        else:
            time.sleep(0.3)
    raise TimeoutError("Never reached SHOP")


def get_hand_labels(state):
    """Return list of card labels from hand."""
    return [c.get("label", "?") for c in state.get("hand", {}).get("cards", [])]


def get_joker_labels(state):
    """Return list of joker labels."""
    return [j.get("label", "?") for j in state.get("jokers", {}).get("cards", [])]


def get_joker_keys(state):
    """Return list of joker keys."""
    return [j.get("key", "?") for j in state.get("jokers", {}).get("cards", [])]


# ── tests ────────────────────────────────────────────────────────

def test_hand_reverse(client):
    """Reverse the hand order and verify it sticks."""
    print("\n--- Test 1: Reverse hand order ---")

    state = wait_for_state(client, ["SELECTING_HAND"])
    hand = state.get("hand", {}).get("cards", [])
    n = len(hand)
    if n < 2:
        print("  SKIP: need at least 2 cards in hand")
        return None

    before = get_hand_labels(state)
    print(f"  Before: {before}")

    # Reverse: [n-1, n-2, ..., 1, 0]
    reverse_order = list(range(n - 1, -1, -1))
    print(f"  Sending rearrange(hand={reverse_order})")
    state = client.call("rearrange", {"hand": reverse_order})

    after_immediate = get_hand_labels(state)
    print(f"  After (immediate): {after_immediate}")

    # Wait a moment and poll again to check persistence
    time.sleep(0.5)
    state = client.call("gamestate")
    after_poll = get_hand_labels(state)
    print(f"  After (poll 0.5s): {after_poll}")

    expected = list(reversed(before))
    immediate_ok = after_immediate == expected
    poll_ok = after_poll == expected

    print(f"  Expected: {expected}")
    print(f"  Immediate match: {immediate_ok}")
    print(f"  Poll match:      {poll_ok}")

    if not immediate_ok:
        print("  >>> FAIL: rearrange response didn't reflect new order")
    if not poll_ok:
        print("  >>> FAIL: align_cards() undid the rearrange on next frame!")

    status = "PASS" if (immediate_ok and poll_ok) else "FAIL"
    print(f"  Result: {status}")
    return {"name": "hand_reverse", "status": status}


def test_hand_rotate(client):
    """Rotate hand by 2 positions and verify."""
    print("\n--- Test 2: Rotate hand by 2 ---")

    state = client.call("gamestate")
    if state.get("state") != "SELECTING_HAND":
        state = wait_for_state(client, ["SELECTING_HAND"])

    hand = state.get("hand", {}).get("cards", [])
    n = len(hand)
    if n < 3:
        print("  SKIP: need at least 3 cards")
        return None

    before = get_hand_labels(state)
    print(f"  Before: {before}")

    # Rotate by 2: [2, 3, ..., n-1, 0, 1]
    rotate_order = list(range(2, n)) + [0, 1]
    print(f"  Sending rearrange(hand={rotate_order})")
    state = client.call("rearrange", {"hand": rotate_order})

    after_immediate = get_hand_labels(state)
    print(f"  After (immediate): {after_immediate}")

    time.sleep(0.5)
    state = client.call("gamestate")
    after_poll = get_hand_labels(state)
    print(f"  After (poll 0.5s): {after_poll}")

    expected = before[2:] + before[:2]
    immediate_ok = after_immediate == expected
    poll_ok = after_poll == expected

    print(f"  Expected: {expected}")
    print(f"  Immediate match: {immediate_ok}")
    print(f"  Poll match:      {poll_ok}")

    status = "PASS" if (immediate_ok and poll_ok) else "FAIL"
    print(f"  Result: {status}")
    return {"name": "hand_rotate", "status": status}


def test_hand_double_rearrange(client):
    """Rearrange twice in a row — second should layer on top of first."""
    print("\n--- Test 3: Double rearrange (reverse then reverse = original) ---")

    state = client.call("gamestate")
    if state.get("state") != "SELECTING_HAND":
        state = wait_for_state(client, ["SELECTING_HAND"])

    hand = state.get("hand", {}).get("cards", [])
    n = len(hand)
    if n < 2:
        print("  SKIP: need at least 2 cards")
        return None

    original = get_hand_labels(state)
    print(f"  Original: {original}")

    # Reverse once
    rev = list(range(n - 1, -1, -1))
    state = client.call("rearrange", {"hand": rev})
    after_first = get_hand_labels(state)
    print(f"  After 1st reverse: {after_first}")

    time.sleep(0.3)

    # Reverse again — should get back to original
    state = client.call("gamestate")
    hand2 = state.get("hand", {}).get("cards", [])
    n2 = len(hand2)
    rev2 = list(range(n2 - 1, -1, -1))
    state = client.call("rearrange", {"hand": rev2})
    after_second = get_hand_labels(state)
    print(f"  After 2nd reverse: {after_second}")

    time.sleep(0.5)
    state = client.call("gamestate")
    after_poll = get_hand_labels(state)
    print(f"  After poll:        {after_poll}")

    ok = after_poll == original
    status = "PASS" if ok else "FAIL"
    if not ok:
        print(f"  >>> FAIL: expected original {original}, got {after_poll}")
    print(f"  Result: {status}")
    return {"name": "hand_double_reverse", "status": status}


def test_joker_rearrange(client):
    """Add 3 jokers, rearrange them, verify order persists."""
    print("\n--- Test 4: Joker rearrange ---")

    # We need to be in SHOP or SELECTING_HAND
    state = client.call("gamestate")
    gs = state.get("state", "")
    if gs not in ("SELECTING_HAND", "SHOP"):
        state = wait_for_state(client, ["SELECTING_HAND", "SHOP"])

    # Clear existing jokers
    jokers = state.get("jokers", {}).get("cards", [])
    for i in range(len(jokers)):
        try:
            client.call("sell", {"joker": 0})
        except APIError:
            pass
    time.sleep(0.3)

    # Add 3 known jokers
    joker_keys = ["j_joker", "j_greedy_joker", "j_lusty_joker"]
    for key in joker_keys:
        try:
            client.call("add", {"key": key})
        except APIError as e:
            print(f"  FAILED to add {key}: {e.message}")
            return None
    time.sleep(0.3)

    state = client.call("gamestate")
    before = get_joker_keys(state)
    before_labels = get_joker_labels(state)
    print(f"  Before: {before_labels}")

    if len(before) < 3:
        print("  SKIP: couldn't add 3 jokers")
        return None

    # Rearrange: reverse [2, 1, 0]
    rev_order = list(range(len(before) - 1, -1, -1))
    print(f"  Sending rearrange(jokers={rev_order})")
    state = client.call("rearrange", {"jokers": rev_order})

    after_immediate = get_joker_keys(state)
    after_labels = get_joker_labels(state)
    print(f"  After (immediate): {after_labels}")

    time.sleep(0.5)
    state = client.call("gamestate")
    after_poll = get_joker_keys(state)
    after_poll_labels = get_joker_labels(state)
    print(f"  After (poll 0.5s): {after_poll_labels}")

    expected = list(reversed(before))
    immediate_ok = after_immediate == expected
    poll_ok = after_poll == expected

    print(f"  Expected keys: {list(reversed(before))}")
    print(f"  Immediate match: {immediate_ok}")
    print(f"  Poll match:      {poll_ok}")

    if not poll_ok:
        print("  >>> FAIL: joker order reverted after align_cards()")

    status = "PASS" if (immediate_ok and poll_ok) else "FAIL"
    print(f"  Result: {status}")
    return {"name": "joker_rearrange", "status": status}


def test_joker_rearrange_in_shop(client):
    """Rearrange jokers while in the SHOP state."""
    print("\n--- Test 5: Joker rearrange in SHOP ---")

    state = advance_to_shop(client)
    give_money(client)

    # Clear and re-add jokers
    state = client.call("gamestate")
    jokers = state.get("jokers", {}).get("cards", [])
    for i in range(len(jokers)):
        try:
            client.call("sell", {"joker": 0})
        except APIError:
            pass
    time.sleep(0.3)

    joker_keys = ["j_joker", "j_banner", "j_mystic_summit"]
    for key in joker_keys:
        try:
            client.call("add", {"key": key})
        except APIError as e:
            print(f"  FAILED to add {key}: {e.message}")
            return None
    time.sleep(0.3)

    state = client.call("gamestate")
    before = get_joker_keys(state)
    before_labels = get_joker_labels(state)
    print(f"  Before: {before_labels}")

    if len(before) < 3:
        print("  SKIP: couldn't add 3 jokers")
        return None

    # Rotate: [1, 2, 0]
    rotate = [1, 2, 0]
    print(f"  Sending rearrange(jokers={rotate})")
    state = client.call("rearrange", {"jokers": rotate})

    after_immediate = get_joker_keys(state)
    after_labels = get_joker_labels(state)
    print(f"  After (immediate): {after_labels}")

    time.sleep(0.5)
    state = client.call("gamestate")
    after_poll = get_joker_keys(state)
    after_poll_labels = get_joker_labels(state)
    print(f"  After (poll 0.5s): {after_poll_labels}")

    expected = [before[1], before[2], before[0]]
    immediate_ok = after_immediate == expected
    poll_ok = after_poll == expected

    print(f"  Expected keys: {expected}")
    print(f"  Immediate match: {immediate_ok}")
    print(f"  Poll match:      {poll_ok}")

    status = "PASS" if (immediate_ok and poll_ok) else "FAIL"
    print(f"  Result: {status}")
    return {"name": "joker_rearrange_shop", "status": status}


def test_hand_rearrange_then_second_rearrange(client):
    """Rearrange hand, poll to confirm, rearrange again to a different order."""
    print("\n--- Test 6: Sequential rearranges to different orders ---")

    state = client.call("gamestate")
    gs = state.get("state", "")
    if gs != "SELECTING_HAND":
        state = wait_for_state(client, ["SELECTING_HAND"])

    hand = state.get("hand", {}).get("cards", [])
    n = len(hand)
    if n < 4:
        print("  SKIP: need at least 4 cards")
        return None

    before = get_hand_labels(state)
    print(f"  Original: {before}")

    # First: move last card to front [n-1, 0, 1, ..., n-2]
    order1 = [n - 1] + list(range(n - 1))
    state = client.call("rearrange", {"hand": order1})
    time.sleep(0.5)
    state = client.call("gamestate")
    after1 = get_hand_labels(state)
    expected1 = [before[n - 1]] + before[:n - 1]
    ok1 = after1 == expected1
    print(f"  After move-last-to-front: {after1}")
    print(f"  Expected:                 {expected1}  {'OK' if ok1 else 'FAIL'}")

    # Second: swap first two cards of the current order [1, 0, 2, 3, ...]
    n2 = len(after1)
    order2 = [1, 0] + list(range(2, n2))
    state = client.call("rearrange", {"hand": order2})
    time.sleep(0.5)
    state = client.call("gamestate")
    after2 = get_hand_labels(state)
    expected2 = [after1[1], after1[0]] + after1[2:]
    ok2 = after2 == expected2
    print(f"  After swap-first-two:     {after2}")
    print(f"  Expected:                 {expected2}  {'OK' if ok2 else 'FAIL'}")

    status = "PASS" if (ok1 and ok2) else "FAIL"
    print(f"  Result: {status}")
    return {"name": "hand_sequential_rearranges", "status": status}


# ── runner ───────────────────────────────────────────────────────

def run_tests(client):
    print("\n" + "=" * 60)
    print("REARRANGE TESTS — card and joker reordering")
    print("=" * 60)

    # Start a fresh game
    print("\n--- Setup: starting fresh game ---")
    try:
        client.call("menu")
    except APIError:
        pass
    time.sleep(0.5)
    client.call("start", {"deck": "RED", "stake": "WHITE", "seed": "REARRANGE1"})
    state = wait_for_state(client, ["SELECTING_HAND"])
    give_money(client)

    results = []

    r = test_hand_reverse(client)
    if r: results.append(r)

    r = test_hand_rotate(client)
    if r: results.append(r)

    r = test_hand_double_rearrange(client)
    if r: results.append(r)

    r = test_joker_rearrange(client)
    if r: results.append(r)

    r = test_joker_rearrange_in_shop(client)
    if r: results.append(r)

    # Get back to SELECTING_HAND for the next test
    state = client.call("gamestate")
    if state.get("state") != "SELECTING_HAND":
        state = wait_for_state(client, ["SELECTING_HAND"])

    r = test_hand_rearrange_then_second_rearrange(client)
    if r: results.append(r)

    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=12346)
    args = parser.parse_args()

    client = BalatroClient("localhost", args.port)
    client.timeout = 20

    results = run_tests(client)

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)

    passes = sum(1 for r in results if r["status"] == "PASS")
    fails = sum(1 for r in results if r["status"] == "FAIL")
    skips = sum(1 for r in results if r["status"] == "SKIP")

    for r in results:
        icon = {"PASS": "OK", "FAIL": "XX", "SKIP": "--"}[r["status"]]
        print(f"  [{icon}] {r['name']}")

    print(f"\n  {passes} PASS, {fails} FAIL, {skips} SKIP out of {len(results)} tests")

    if fails > 0:
        print("\n  Failures indicate align_cards() is still undoing rearrangements.")
        print("  Check that rearrange.lua sets card.rank and calls set_ranks() + align_cards().")
        sys.exit(1)


if __name__ == "__main__":
    main()
