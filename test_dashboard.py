"""Quick end-to-end test of the dashboard client against the live API."""

import os
os.environ.setdefault("JACKPOTTS_URL", "https://jackpotts.drlat.dev")

# Load .env.local (same loader the supervisor uses)
import src.balatro_bot.config  # noqa: F401 — side effect: loads .env.local

from src.balatro_bot.dashboard_client import (
    post_batch_start,
    post_instance_states,
    post_game,
    post_batch_finish,
)

print("=== Dashboard Client E2E Test ===\n")

# 1. Start batch
batch_id = post_batch_start(
    batch_number=9999,
    num_instances=2,
    games_per_inst=10,
    decks="RED,BLUE",
    stake="WHITE",
)
print(f"1. Batch start:  batch_id={batch_id}")
assert batch_id is not None, "Failed to create batch"

# 2. Upsert instances
post_instance_states(batch_id, [
    {"port": 12346, "name": "Rapunzel", "deck": "RED", "state": "running",
     "games_completed": 3, "games_total": 10, "wins": 1, "restarts": 0, "last_reason": ""},
    {"port": 12347, "name": "Flynn", "deck": "BLUE", "state": "running",
     "games_completed": 5, "games_total": 10, "wins": 2, "restarts": 0, "last_reason": ""},
])
print("2. Instances:    upserted 2 slots")

# 3. Ingest a game
post_game(batch_id, {
    "instance_port": 12346,
    "seed": "TEST9999",
    "deck": "RED",
    "stake": "WHITE",
    "win": True,
    "final_ante": 8,
    "final_round": 24,
    "actions": 120,
    "final_money": 42,
    "final_jokers": "Misprint, Blueprint",
    "total_hands": 30,
    "total_discards": 12,
    "rounds": [
        {"ante": 1, "blind_name": "Small Blind", "is_boss": False,
         "scored": 500, "needed": 300, "won": True, "hands_used": 1, "discards_used": 0},
        {"ante": 1, "blind_name": "Big Blind", "is_boss": False,
         "scored": 800, "needed": 450, "won": True, "hands_used": 2, "discards_used": 1},
        {"ante": 1, "blind_name": "The Psychic", "is_boss": True,
         "scored": 1200, "needed": 800, "won": True, "hands_used": 2, "discards_used": 0},
    ],
    "jokers": [
        {"joker_name": "Misprint", "in_final_roster": True, "buy_ante": 1},
        {"joker_name": "Blueprint", "in_final_roster": True, "buy_ante": 2, "final_value": 3.0},
    ],
    "hand_types": {"Flush": 8, "Pair": 12, "High Card": 5},
})
print("3. Game:         ingested 1 game (3 rounds, 2 jokers, 3 hand types)")

# 4. Finish batch
post_batch_finish(batch_id, status="finished")
print("4. Batch finish: marked finished")

print("\n=== All 4 endpoints OK ===")
print(f"\nCleanup: DELETE FROM batches WHERE batch_number = 9999;")
