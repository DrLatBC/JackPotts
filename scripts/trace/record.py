"""Play seeded games on a running simulator and record what the bot did.

    python scripts/trace/record.py PORT OUT.json [SEED[@DECK] ...]

With no seeds given, plays the reference list in seeds.txt. The trace holds
every action sent to the simulator (method, params, resulting state, money,
chips) and every game log line without its timestamp. Two traces of the same
seeds from different code are compared with compare.py: a change that should
not alter behaviour must leave both identical.

Start a simulator first:
    .venv-jackdaw/Scripts/python.exe sim/jackdaw_adapter/serve.py --port PORT
"""
from __future__ import annotations

import json
import logging
import sys
import tempfile
import time
from pathlib import Path

from balatro_bot.bot import run_bot, setup_logging, wait_for_server
from balatro_bot.engine import RuleEngine
from balatro_bot.infrastructure.client import BalatroClient

READ_ONLY = ("gamestate", "health", "menu")


def reference_seeds() -> list[str]:
    lines = (Path(__file__).parent / "seeds.txt").read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")]


def main() -> None:
    port, out = int(sys.argv[1]), sys.argv[2]
    seeds = sys.argv[3:] or reference_seeds()

    logs = Path(tempfile.mkdtemp(prefix="trace_"))
    game_log = logs / "game.log"
    setup_logging(False, log_file=str(game_log), wins_file=str(logs / "wins.log"),
                  scoring_file=str(logs / "scoring.log"))
    logging.getLogger().handlers[0].setLevel(logging.ERROR)

    client = BalatroClient(host="127.0.0.1", port=port)
    engine = RuleEngine()
    wait_for_server(client)

    trace: list = []
    spent = {"rpc": 0.0, "decide": 0.0}
    send, decide = client.call, engine.decide

    def timed_call(method, params=None):
        start = time.perf_counter()
        try:
            result = send(method, params)
        finally:
            spent["rpc"] += time.perf_counter() - start
        if method not in READ_ONLY:
            trace.append([method, params, result.get("state"), result.get("money"),
                          (result.get("round") or {}).get("chips")])
        return result

    def timed_decide(state):
        start = time.perf_counter()
        try:
            return decide(state)
        finally:
            spent["decide"] += time.perf_counter() - start

    object.__setattr__(client, "call", timed_call)
    engine.decide = timed_decide

    began = time.perf_counter()
    for entry in seeds:
        seed, _, deck = entry.partition("@")
        trace.append(["GAME", seed, deck or "BLUE"])
        run_bot(client, engine, start_game=True, deck=deck or "BLUE", stake="WHITE", seed=seed)
    wall = time.perf_counter() - began

    for handler in logging.getLogger().handlers:
        handler.flush()
    with open(game_log, encoding="utf-8") as f:
        lines = [ln.split(" ", 1)[1].rstrip("\n") if " " in ln else ln.rstrip("\n") for ln in f]
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"trace": trace, "log": lines}, f)

    print(f"{len(seeds)} games in {wall:.1f}s ({3600 * len(seeds) / wall:.0f}/hour): "
          f"deciding {spent['decide']:.1f}s, simulator calls {spent['rpc']:.1f}s")
    print(f"{len(trace)} trace entries, {len(lines)} log lines -> {out}")


if __name__ == "__main__":
    main()
