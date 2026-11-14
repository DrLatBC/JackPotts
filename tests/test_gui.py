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


def test_simulator_toggle_adds_sim_flag():
    assert "--sim" not in gui.supervisor_command(gui.DEFAULTS)
    assert gui.supervisor_command({**gui.DEFAULTS, "sim": True})[-1] == "--sim"


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


GAME_LOG = """\
16:15:09 [INFO] Started new game: deck=BLUE stake=WHITE seed=UDABG75D
16:15:09 [INFO] [ANTE 1] Roster (0 jokers): [none]
16:15:09 [INFO] Blind: Small Blind (need 300 chips)
16:19:40 [INFO] [ANTE 6] Money: $12
16:19:41 [INFO] Blind: The Serpent (need 40,000 chips)
"""
GAME_OVER = "16:20:06 [INFO] Game over: DEFEAT | seed=UDABG75D ante=6 round=18 | 195 actions taken\n"


@pytest.fixture
def sup(status_file, tmp_path, monkeypatch):
    monkeypatch.setattr(supervisor, "LOG_DIR", tmp_path)
    (tmp_path / str(supervisor.BASE_PORT)).mkdir()
    return supervisor.Supervisor(n=1, games=10, decks=["BLUE"], stake="WHITE", seed=None,
                                 explore_instances=1, explore_rate=0.15)


def _feed_log(slot, path, text):
    with path.open("a", encoding="utf-8") as fh:
        fh.write(text)
    slot.log_file = path
    slot.poll_log_for_softlock()


def test_status_follows_the_current_game_and_records_finished_ones(sup, tmp_path):
    slot = sup.slots[0]
    log_path = tmp_path / "game.log"
    _feed_log(slot, log_path, GAME_LOG)
    live = sup._status("running")["slots"][0]
    assert (live["ante"], live["blind"]) == (6, "The Serpent")

    _feed_log(slot, log_path, GAME_OVER)
    status = sup._status("running")
    assert (status["slots"][0]["ante"], status["slots"][0]["blind"]) == (0, "")
    [game] = status["recent_games"]
    assert (game["name"], game["won"], game["ante"], game["seed"]) == (slot.name, False, 6, "UDABG75D")


def test_panel_explore_switch_reaches_the_status_and_the_bot(sup, tmp_path, monkeypatch):
    monkeypatch.setattr(gui, "LOG_DIR", tmp_path)
    slot = sup.slots[0]
    slot.explore_active = 0.15
    gui.set_explore_rate(slot.port, 0.0)
    live = sup._status("running")["slots"][0]
    assert (live["explore_rate"], live["explore_active"]) == (0.0, 0.15)

    _feed_log(slot, tmp_path / "game.log", "16:30:00 [INFO] Explore rate: 0.00\n")
    assert sup._status("running")["slots"][0]["explore_active"] == 0.0


def test_relaunched_bot_gets_the_rate_file_and_reads_its_log_from_the_end(sup, tmp_path, monkeypatch):
    launched = []
    monkeypatch.setattr(supervisor.subprocess, "Popen", lambda cmd, **kw: launched.append(cmd))
    slot = sup.slots[0]
    log_path = tmp_path / str(slot.port) / f"game_{sup.session_num:03d}.log"
    log_path.write_text(GAME_LOG + GAME_OVER, encoding="utf-8")
    sup.launch_bot(slot)
    cmd = launched[0]
    assert cmd[cmd.index("--explore-file") + 1] == str(slot.explore_file)
    assert cmd[cmd.index("--explore") + 1] == "0.15"
    assert slot.log_offset == log_path.stat().st_size
    slot.poll_log_for_softlock()
    assert not slot.finished_games


def test_bot_switches_rate_from_the_file_before_a_game(tmp_path, monkeypatch):
    from balatro_bot import cli
    from balatro_bot.domain.policy import exploration
    monkeypatch.setattr(exploration, "_explorer", exploration.Explorer(0.15))
    path = tmp_path / "explore_rate.txt"
    cli.apply_explore_file(path)
    assert exploration.explorer().rate == 0.15
    for bad in ("garbage", "1.5"):
        path.write_text(bad)
        cli.apply_explore_file(path)
        assert exploration.explorer().rate == 0.15
    path.write_text("0.0")
    cli.apply_explore_file(path)
    assert exploration.explorer().rate == 0.0


def test_panel_notifies_once_when_its_batch_ends(monkeypatch):
    sent = []
    final = {"phase": "stopped", "session": 178, "completed": 120, "total_expected": 120, "wins": 6}
    monkeypatch.setattr(gui, "read_status_file", lambda: final)
    tray = type("T", (), {"notify": lambda self, title, msg: sent.append((title, msg))})()
    panel = type("P", (), {"watched_session": None, "tray": tray})()
    watch = gui.ControlPanel._watch_for_finish

    watch(panel, None)
    assert not sent
    watch(panel, {"session": 178})
    watch(panel, None)
    watch(panel, None)
    assert sent == [("Batch #178 ended", "120 / 120 games  ·  6 wins  5.0%")]


def test_tray_clicks_and_second_launch_open_the_panel():
    import tkinter as tk

    root = tk.Tk()
    root.withdraw()
    events = []
    tray = gui.TrayIcon(on_open=lambda: events.append("open"), on_quit=lambda: events.append("quit"))
    # Messages go to this tray's own window: show_running_panel() would find a real open panel first.
    try:
        gui._user32.PostMessageW(tray.hwnd, gui.WM_TRAY, 0, 0x202)  # left click
        gui._user32.PostMessageW(tray.hwnd, gui.WM_SHOW_PANEL, 0, 0)
        for _ in range(20):
            root.update()
        assert events == ["open", "open"]
    finally:
        tray.close()
        root.destroy()


def test_batch_children_run_below_normal_priority_but_stream_does_not(sup):
    import subprocess

    assert sup._child_flags() & subprocess.BELOW_NORMAL_PRIORITY_CLASS
    assert sup._child_flags() & subprocess.CREATE_NO_WINDOW
    sup.stream = True
    assert not sup._child_flags() & subprocess.BELOW_NORMAL_PRIORITY_CLASS


@pytest.fixture
def no_launch(tmp_path, monkeypatch):
    """Point the panel's files at tmp_path and record supervisor launches instead of running them."""
    launches = []
    monkeypatch.setattr(gui, "LOG_DIR", tmp_path)
    monkeypatch.setattr(gui, "SETTINGS_FILE", tmp_path / "gui_settings.json")
    monkeypatch.setattr(gui, "STOP_FILE", tmp_path / "supervisor.stop")
    monkeypatch.setattr(gui, "STATUS_FILE", tmp_path / "supervisor_status.json")
    monkeypatch.setattr(gui, "CONSOLE_LOG", tmp_path / "supervisor_console.log")
    monkeypatch.setattr(gui.subprocess, "Popen", lambda cmd, **kw: launches.append((cmd, kw)))
    return launches


def test_launched_batch_has_no_window(no_launch):
    gui.launch_batch({**gui.DEFAULTS, "sim": True})
    cmd, kw = no_launch[0]
    assert kw["creationflags"] & gui.subprocess.CREATE_NO_WINDOW
    assert cmd[-1] == "--sim"


def test_start_saved_batch_uses_saved_settings(no_launch, monkeypatch):
    monkeypatch.setattr(gui, "read_status", lambda: None)
    monkeypatch.setattr(gui, "ports_in_use", lambda: False)
    gui.SETTINGS_FILE.write_text(json.dumps({**gui.DEFAULTS, "instances": 3}))
    assert gui.start_saved_batch()
    assert no_launch[0][0][no_launch[0][0].index("-n") + 1] == "3"


@pytest.mark.parametrize("status, busy", [({"phase": "running"}, False), (None, True)])
def test_start_saved_batch_leaves_a_running_batch_alone(no_launch, monkeypatch, status, busy):
    monkeypatch.setattr(gui, "read_status", lambda: status)
    monkeypatch.setattr(gui, "ports_in_use", lambda: busy)
    assert not gui.start_saved_batch()
    assert no_launch == []
