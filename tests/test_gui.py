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


NETSTAT = """
Active Connections

  Proto  Local Address          Foreign Address        State           PID
  TCP    127.0.0.1:12346        0.0.0.0:0              LISTENING       4120
  TCP    127.0.0.1:12346        127.0.0.1:50211        ESTABLISHED     4120
  TCP    0.0.0.0:135            0.0.0.0:0              LISTENING       1032
  TCP    127.0.0.1:50300        127.0.0.1:12347        TIME_WAIT       0
  TCP    [::]:445               [::]:0                 LISTENING       4
"""


def test_listening_ports_reads_only_listeners():
    assert gui.listening_ports(NETSTAT) == {12346, 135, 445}


def test_ports_in_use_never_connects(monkeypatch):
    import socket
    monkeypatch.setattr(socket, "create_connection",
                        lambda *a, **k: pytest.fail("ports_in_use must not connect to game ports"))
    monkeypatch.setattr(gui.subprocess, "run",
                        lambda *a, **k: type("R", (), {"stdout": NETSTAT})())
    assert gui.ports_in_use() is True
