"""Replay saved simulator games and check each ends where the bot's game did.

    .venv-jackdaw\\Scripts\\python.exe scripts/replay_check.py bot_log/12346/replay_185.jsonl.gz [...]

Run it with the simulator's interpreter: it needs Jackdaw, and of the bot only the
replay file reader.
For every game in every file it starts a fresh game with the recorded seed, sends
the recorded requests, and compares the final ante, round and money with what the
replay says. Exits 1 if any game ends differently or cannot be replayed.
"""
from __future__ import annotations

import importlib.metadata
import sys
import time
from pathlib import Path

from jackdaw.bridge import RPCError, SimBackend

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from balatro_bot.replays import read_replays  # noqa: E402


def main() -> int:
    version = importlib.metadata.version("jackdaw")
    sim = SimBackend()
    games = actions = bad = other_version = 0
    started = time.perf_counter()
    for path in sys.argv[1:]:
        for record in read_replays(path):
            games += 1
            if record.get("simulator") != f"jackdaw {version}":
                other_version += 1
            try:
                sim.handle("menu", None)
                for method, params in record["actions"]:
                    sim.handle(method, params)
                    actions += 1
                state = sim.handle("gamestate", None)
            except RPCError as e:
                bad += 1
                print(f"{path}: seed {record['seed']} stopped replaying: {e.message}")
                continue
            ended = {"ante": state.get("ante_num", 0), "round": state.get("round_num", 0),
                     "money": state.get("money", 0)}
            if state.get("state") != "GAME_OVER" or ended != record["final"]:
                bad += 1
                print(f"{path}: seed {record['seed']} ended at {state.get('state')} {ended}, "
                      f"recorded {record['final']}")
    elapsed = time.perf_counter() - started
    print(f"{games} games, {actions} actions replayed in {elapsed:.1f}s; {bad} did not match")
    if other_version:
        print(f"{other_version} games were recorded on a different simulator version than jackdaw {version}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
