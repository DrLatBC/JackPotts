"""The stream launcher with stand-in processes: how each way a stream ends is reported."""

import json
from argparse import Namespace

import pytest

from balatro_bot import jobs, stream


class FakeProc:
    """A process that prints ``output`` when started, runs ``each_poll`` whenever it is
    polled, and exits after ``polls`` polls (never, with None)."""

    def __init__(self, polls=None, code=0, output="", each_poll=None):
        self.polls, self.returncode, self.pid = polls, None, 4242
        self.code, self.output, self.each_poll = code, output, each_poll

    def poll(self):
        if self.each_poll:
            self.each_poll()
        if self.polls is not None:
            self.polls -= 1
            if self.polls <= 0:
                self.returncode = self.code
        return self.returncode


@pytest.fixture
def launcher(tmp_path, monkeypatch):
    """The stream's job folder in tmp_path, with stand-ins for everything it starts."""
    monkeypatch.setattr(jobs, "ROOT", tmp_path)
    folder = jobs.stream().folder
    monkeypatch.setattr(stream.jobs, "tie_children", lambda: True)
    monkeypatch.setattr(stream.jobs, "kill_port_processes", lambda ports: 0)
    server = stream.game_server
    monkeypatch.setattr(server, "ensure_isolated_profile", lambda port: str(tmp_path / "profile"))
    monkeypatch.setattr(server, "graceful_kill", lambda proc: None)
    monkeypatch.setattr(server, "unlock_all", lambda profile_dir, love_path: None)
    for path in ("love_path", "lovely_path", "balatrobot_path"):
        monkeypatch.setitem(server.INSTALL.__dict__, path, path)
    monkeypatch.setattr(stream.time, "sleep", lambda s: None)
    monkeypatch.setattr(stream.subprocess, "run", lambda *a, **k: None)
    procs = {}

    def popen(cmd, **kw):
        kind = "player" if "rl.stream" in cmd else "game"
        procs[kind + "_cmd"] = cmd
        kw["stdout"].write(procs[kind].output)
        kw["stdout"].flush()
        return procs[kind]

    monkeypatch.setattr(stream.subprocess, "Popen", popen)
    args = Namespace(model="rl/data/ppo/r1/latest.pt", follow=True, deck="BLUE", stake="WHITE", delay=1.5,
                     port=12340, overlay_port=0)
    return folder, procs, args


def status(tmp_path):
    return json.loads((tmp_path / "status.json").read_text())


def test_player_command_runs_rl_stream_under_the_rl_venv():
    args = Namespace(model="m.pt", follow=True, deck="RED", stake="GOLD", delay=0.5, port=12340)
    cmd = stream.player_command(args)
    assert ".venv-rl" in cmd[0] and cmd[1:3] == ["-m", "rl.stream"]
    assert cmd[cmd.index("--port") + 1] == "12340" and cmd[-1] == "--follow"
    assert "--follow" not in stream.player_command(Namespace(**{**vars(args), "follow": False}))


def test_a_stop_before_the_game_opens(launcher, monkeypatch):
    tmp_path, procs, args = launcher
    procs["game"] = FakeProc()
    monkeypatch.setattr(stream.game_server, "listening", lambda port: (tmp_path / "stop").touch())
    assert stream.run(args) == 1
    assert status(tmp_path)["phase"] == "stopped"
    assert "player_cmd" not in procs


def test_a_game_that_never_opens_fails_with_its_last_line(launcher, monkeypatch):
    tmp_path, procs, args = launcher
    procs["game"] = FakeProc(polls=1, code=1, output="starting\nLovely: could not inject\n")
    monkeypatch.setattr(stream.game_server, "listening", lambda port: False)
    assert stream.run(args) == 1
    assert status(tmp_path)["phase"] == "failed"
    assert status(tmp_path)["error"] == "Lovely: could not inject"


def test_a_stopped_player_ends_the_stream_as_stopped(launcher, monkeypatch):
    tmp_path, procs, args = launcher
    procs["game"] = FakeProc()
    procs["player"] = FakeProc(polls=3, each_poll=(tmp_path / "stop").touch)
    monkeypatch.setattr(stream.game_server, "listening", lambda port: True)
    assert stream.run(args) == 0
    assert status(tmp_path)["phase"] == "stopped"
    assert not (tmp_path / "stop").exists()
    assert procs["player_cmd"][procs["player_cmd"].index("--model") + 1] == args.model


def test_a_player_that_dies_fails_the_stream(launcher, monkeypatch):
    tmp_path, procs, args = launcher
    procs["game"] = FakeProc()
    procs["player"] = FakeProc(polls=2, code=1, output="Traceback\nSystemExit: 3 games in a row ended in an error\n")
    monkeypatch.setattr(stream.game_server, "listening", lambda port: True)
    assert stream.run(args) == 1
    s = status(tmp_path)
    assert s["phase"] == "failed" and s["error"].startswith("SystemExit: 3 games")


def test_closing_the_game_window_stops_the_stream(launcher, monkeypatch):
    tmp_path, procs, args = launcher
    procs["game"], procs["player"] = FakeProc(polls=4), FakeProc()
    monkeypatch.setattr(stream.game_server, "listening", lambda port: True)
    assert stream.run(args) == 0
    s = status(tmp_path)
    assert s["phase"] == "stopped" and s["note"] == "the game window was closed"


def test_the_overlay_is_served_while_the_stream_runs(launcher, monkeypatch):
    import urllib.request

    tmp_path, procs, args = launcher
    (tmp_path / "now.json").unlink(missing_ok=True)
    seen = {}

    def check():
        url = status(tmp_path).get("overlay")
        if url and "page" not in seen:
            with urllib.request.urlopen(url, timeout=5) as r:
                seen["page"] = r.status
            (tmp_path / "stop").touch()

    procs["game"] = FakeProc()
    procs["player"] = FakeProc(polls=5, each_poll=check)
    monkeypatch.setattr(stream.game_server, "listening", lambda port: True)
    assert stream.run(args) == 0
    assert seen["page"] == 200
    with pytest.raises(OSError):
        urllib.request.urlopen(status(tmp_path)["overlay"], timeout=2)


def test_a_taken_overlay_port_leaves_the_stream_running(launcher, monkeypatch):
    tmp_path, procs, args = launcher
    procs["game"] = FakeProc()
    procs["player"] = FakeProc(polls=3, each_poll=(tmp_path / "stop").touch)
    monkeypatch.setattr(stream.game_server, "listening", lambda port: True)

    def taken(folder, port):
        raise OSError("in use")

    monkeypatch.setattr(stream.overlay, "serve", taken)
    assert stream.run(args) == 0
    s = status(tmp_path)
    assert s["phase"] == "stopped" and "overlay" not in s and "in use" in s["overlay error"]
