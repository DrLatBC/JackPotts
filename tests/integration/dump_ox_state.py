"""Dump the full gamestate JSON while sitting at The Ox boss blind."""

import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from balatrobot.cli.client import BalatroClient

client = BalatroClient(port=12846)
state = client.call("gamestate")

# Dump blinds section specifically
print("=== BLINDS ===")
print(json.dumps(state.get("blinds", {}), indent=2))

# Dump full state to file for inspection
with open("ox_state_dump.json", "w") as f:
    json.dump(state, f, indent=2)

print(f"\nFull state written to ox_state_dump.json ({len(json.dumps(state))} bytes)")
print(f"Game state: {state.get('state')}")
print(f"Money: {state.get('money')}")
