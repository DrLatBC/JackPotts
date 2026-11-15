"""Replays: what the client records and what a replay file holds."""
from __future__ import annotations

import balatrobot

from balatro_bot.infrastructure.client import BalatroClient
from balatro_bot.replays import REPLAY_VERSION, ReplayWriter, read_replays, replay_record


def _client(monkeypatch, fail_on: str | None = None) -> BalatroClient:
    def call(self, method, params=None):
        if method == fail_on:
            raise balatrobot.APIError(name="NOT_ALLOWED", message="no", code=-1)
        return {"state": "SHOP"}

    monkeypatch.setattr(balatrobot.BalatroClient, "call", call)
    return BalatroClient(host="127.0.0.1", port=1)


def test_nothing_is_recorded_until_a_recording_starts(monkeypatch):
    client = _client(monkeypatch)
    client.call("buy", {"card": 0})
    assert client.recording is None


def test_only_requests_that_change_the_game_are_recorded(monkeypatch):
    client = _client(monkeypatch)
    client.recording = []
    client.call("gamestate")
    client.call("buy", {"card": 0})
    client.call("health")
    client.call("next_round")
    assert client.recording == [["buy", {"card": 0}], ["next_round", None]]


def test_a_refused_request_is_not_recorded(monkeypatch):
    client = _client(monkeypatch, fail_on="use")
    client.recording = []
    try:
        client.call("use", {"consumable": 0})
    except balatrobot.APIError:
        pass
    client.call("play", {"cards": [0, 1]})
    assert client.recording == [["play", {"cards": [0, 1]}]]


def _record(seed: str, **extra) -> dict:
    return replay_record(
        actions=[["start", {"deck": "BLUE", "stake": "WHITE", "seed": seed}], ["select", None]],
        explored=[1], seed=seed, deck="BLUE", stake="WHITE", simulator="jackdaw 0.2.6", won=False,
        final_state={"ante_num": 3, "round_num": 7, "money": 12}, **extra,
    )


def test_a_replay_holds_the_seed_the_actions_and_where_the_game_ended():
    record = _record("ABC12345", batch=491, explore_rate=0.15)
    assert record["v"] == REPLAY_VERSION
    assert record["actions"][0] == ["start", {"deck": "BLUE", "stake": "WHITE", "seed": "ABC12345"}]
    assert record["final"] == {"ante": 3, "round": 7, "money": 12}
    assert (record["batch"], record["explore_rate"], record["explored"]) == (491, 0.15, [1])
    assert "batch" not in _record("ABC12345") and "explore_rate" not in _record("ABC12345")


def test_replays_survive_a_restart_of_the_writer(tmp_path):
    path = tmp_path / "replay_001.jsonl.gz"
    first = ReplayWriter(path)
    first.write(_record("AAAA1111"))
    first.write(_record("BBBB2222"))
    # The first writer is never closed, as when a bot dies: its games are already on disk.
    second = ReplayWriter(path)
    second.write(_record("CCCC3333"))
    second.close()
    first.close()
    assert [r["seed"] for r in read_replays(path)] == ["AAAA1111", "BBBB2222", "CCCC3333"]


def test_a_game_cut_off_mid_write_does_not_spoil_the_ones_before_it(tmp_path):
    import gzip

    path = tmp_path / "replay_001.jsonl.gz"
    writer = ReplayWriter(path)
    writer.write(_record("AAAA1111"))
    writer.write(_record("BBBB2222"))
    writer.close()
    whole = gzip.compress(b'{"seed":"CCCC3333"}')
    with open(path, "ab") as f:
        f.write(whole[: len(whole) // 2])
    assert [r["seed"] for r in read_replays(path)] == ["AAAA1111", "BBBB2222"]


def test_a_replay_file_reads_as_plain_gzipped_json_lines(tmp_path):
    import gzip
    import json

    path = tmp_path / "replay_001.jsonl.gz"
    writer = ReplayWriter(path)
    writer.write(_record("AAAA1111"))
    writer.write(_record("BBBB2222"))
    writer.close()
    with gzip.open(path, "rt", encoding="utf-8") as f:
        assert [json.loads(line)["seed"] for line in f] == ["AAAA1111", "BBBB2222"]
