"""Control panel: supervisor command, status reading, supervisor status snapshot."""

import json
import os

import pytest

from balatro_bot import gui, supervisor


@pytest.fixture
def status_file(tmp_path, monkeypatch):
    path = tmp_path / "supervisor_status.json"
    monkeypatch.setattr(gui, "STATUS_FILE", path)
    monkeypatch.setattr(supervisor, "STATUS_FILE", path)
    return path


def test_command_without_exploration():
    cmd = gui.supervisor_command({**gui.DEFAULTS, "explore_instances": 0, "deck": "RED"})
    assert cmd[1:] == ["-m", "balatro_bot.supervisor", "-n", "6", "--games", "2000",
                       "--deck", "RED", "--stake", "WHITE"]


def test_command_with_exploration():
    cmd = gui.supervisor_command(gui.DEFAULTS)
    assert cmd[-4:] == ["--explore-instances", "2", "--explore-rate", "0.15"]


def test_status_of_a_live_supervisor_is_read(status_file):
    status_file.write_text(json.dumps({"phase": "running", "pid": os.getpid()}))
    assert gui.read_status()["phase"] == "running"


@pytest.mark.parametrize("payload", [
    {"phase": "stopped", "pid": os.getpid()},
    {"phase": "running", "pid": 999999999},
])
def test_finished_or_dead_supervisor_reads_as_none(status_file, payload):
    status_file.write_text(json.dumps(payload))
    assert gui.read_status() is None


def test_missing_or_garbled_status_reads_as_none(status_file):
    assert gui.read_status() is None
    status_file.write_text("{not json")
    assert gui.read_status() is None


def test_supervisor_status_snapshot_marks_exploring_slots(status_file, tmp_path, monkeypatch):
    monkeypatch.setattr(supervisor, "LOG_DIR", tmp_path)
    sup = supervisor.Supervisor(n=3, games=10, decks=["BLUE"], stake="WHITE", seed=None,
                                explore_instances=1, explore_rate=0.15)
    sup._write_status(sup._status("running"))
    written = json.loads(status_file.read_text())
    assert written["phase"] == "running"
    assert written["pid"] == os.getpid()
    assert [s["explore_rate"] for s in written["slots"]] == [0.15, 0.0, 0.0]
    assert written["total_expected"] == 30
