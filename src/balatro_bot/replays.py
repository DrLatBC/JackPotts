"""Replays: a simulator game kept as its seed and the requests that changed it.

The simulator is deterministic, so sending the same requests to a fresh game with
the same seed walks through the same states. A replay therefore stands in for
everything the game saw and did, in a few hundred bytes, and any view of it can be
rebuilt later (``scripts/replay_check.py`` replays a file and checks where each game
ends). Live Balatro games cannot be replayed this way and keep their full records.

A replay file is one gzip member per game, each holding one JSON object, so a game
is either wholly in the file or not at all and ``gzip`` tools read the file as
JSON lines:

    v           format version
    seed, deck, stake
    simulator   name and version the game ran on; replay on the same version
    batch       dashboard batch id, when the game belonged to one
    actions     [[method, params], ...] in order, starting with "start"
    explored    positions in ``actions`` of exploration picks (not the bot's own choice)
    explore_rate  the instance's exploration rate, when exploring
    won         the bot's own verdict (reached ante 9)
    final       {"ante", "round", "money"} when the game ended
"""

from __future__ import annotations

import gzip
import json
import zlib
from pathlib import Path
from typing import Any, Iterator

REPLAY_VERSION = 1


class ReplayWriter:
    """Appends replays to one file, each game in a single write. A bot that is killed
    loses nothing already finished, and a restarted bot appends to the same file."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._file = open(self.path, "ab")

    def write(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, separators=(",", ":")) + "\n"
        self._file.write(gzip.compress(line.encode("utf-8"), mtime=0))
        self._file.flush()

    def close(self) -> None:
        self._file.close()


def read_replays(path: str | Path) -> Iterator[dict[str, Any]]:
    """Every complete replay in a file, in the order the games were played. A game
    cut off mid-write (the bot was killed during it) ends the file."""
    data = Path(path).read_bytes()
    while data:
        member = zlib.decompressobj(wbits=31)
        try:
            line = member.decompress(data)
        except zlib.error:
            return
        if not member.eof:
            return
        yield json.loads(line)
        data = member.unused_data


def replay_record(
    *,
    actions: list,
    explored: list[int],
    seed: str,
    deck: str,
    stake: str,
    simulator: str,
    won: bool,
    final_state: dict[str, Any],
    batch: int | None = None,
    explore_rate: float = 0.0,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "v": REPLAY_VERSION, "seed": seed, "deck": deck, "stake": stake, "simulator": simulator,
        "actions": actions, "explored": explored, "won": won,
        "final": {
            "ante": final_state.get("ante_num", 0), "round": final_state.get("round_num", 0),
            "money": final_state.get("money", 0),
        },
    }
    if batch is not None:
        record["batch"] = batch
    if explore_rate > 0:
        record["explore_rate"] = explore_rate
    return record
