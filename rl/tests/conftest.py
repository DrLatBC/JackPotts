"""Shared fixtures: five bot games from batch 186 (Blue deck, White stake, Jackdaw
0.2.6): two wins, an ante 1 loss, an ante 5 loss and an ante 3 loss with an Aura pick."""

from __future__ import annotations

from pathlib import Path

import pytest

from balatro_bot.replays import read_replays
from rl import dataset
from rl.convert import convert_batch, convert_game

FIXTURE = Path(__file__).parent / "data" / "replays.jsonl.gz"


@pytest.fixture(scope="session")
def records() -> list[dict]:
    return list(read_replays(FIXTURE))


@pytest.fixture(scope="session")
def converted(records):
    """Each fixture game as ``(record, examples, stats)``, converted with the bot
    interface replaying alongside: the game's state must match it after every request."""
    return [(r, *convert_game(r, cross_check=True)) for r in records]


@pytest.fixture(scope="session")
def shard(records, tmp_path_factory) -> dataset.Examples:
    path = tmp_path_factory.mktemp("shards") / "shard_0000.npz"
    stats, errors = convert_batch((path, [("fixture", r) for r in records], False))
    assert errors == []
    assert stats["games"] == len(records)
    return dataset.load_shard(path)
