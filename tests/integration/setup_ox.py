"""Launch game, advance to The Ox boss blind, then stop for manual inspection."""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from balatrobot.cli.client import BalatroClient, APIError
from harness import (
    TEST_PORT, wait_for_state, ensure_server,
    advance_to_boss_select, force_boss, beat_blind_fast,
    get_current_blind, cheat_win_if_needed,
)

port = TEST_PORT
client, server_proc = ensure_server(port, headless=False)

print("Waiting for game to fully load...")
time.sleep(5)

try:
    client.call("menu")
except Exception:
    time.sleep(3)
    client.call("menu")
time.sleep(1)

client.call("start", {"deck": "RED", "stake": "WHITE", "seed": "OX_LOOK"})
state = wait_for_state(client, {"SELECTING_HAND"})

# Play 3 Pair hands to build up played counts, then cheat to win
print("Playing 3 Pair hands to build up played counts...")
for i in range(3):
    state = client.call("gamestate")
    if state.get("state") != "SELECTING_HAND":
        break
    hc = state.get("hand", {}).get("cards", [])
    if hc:
        try:
            client.call("discard", {"cards": list(range(min(len(hc), 5)))})
            time.sleep(0.3)
        except APIError:
            pass
    for key in ["H_K", "D_K", "C_3", "S_5", "H_7"]:
        try:
            client.call("add", {"key": key})
        except APIError:
            pass
    time.sleep(0.3)
    state = client.call("gamestate")
    hc = state.get("hand", {}).get("cards", [])
    indices = list(range(len(hc) - 5, len(hc)))
    try:
        client.call("play", {"cards": indices})
    except APIError:
        pass
    time.sleep(0.5)
    for _ in range(20):
        state = client.call("gamestate")
        if state.get("state") in ("SELECTING_HAND", "BLIND_SELECT", "SHOP", "GAME_OVER"):
            break
        time.sleep(0.3)

# Cheat to win the blind
blind_name, _ = get_current_blind(client.call("gamestate"))
cheat_win_if_needed(client, blind_name)

print("Advancing to ante 6 boss select...")
state = advance_to_boss_select(client, target_ante=6)

if not force_boss(client, "The Ox"):
    print("Could not force The Ox")
    sys.exit(1)

print("\nThe Ox is set as boss. Selecting blind...")
client.call("select")
time.sleep(0.5)
state = wait_for_state(client, {"SELECTING_HAND"})

hand_levels = state.get("hands", {})
print("\nHand played counts:")
for ht, data in sorted(hand_levels.items()):
    if isinstance(data, dict) and data.get("played", 0) > 0:
        print(f"  {ht}: {data['played']}")

print(f"\nMoney: {state.get('money')}")
import json
print("\n=== BLINDS ===")
print(json.dumps(state.get("blinds", {}), indent=2))

with open("ox_state_dump.json", "w") as f:
    json.dump(state, f, indent=2)
print(f"\nFull state written to ox_state_dump.json")

print("\nGame is paused at The Ox. Go look at it.")
print("Press Enter to quit...")
input()
