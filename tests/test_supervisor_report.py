"""The supervisor watching its instances through their reports: counts, recent games, restarts."""

from __future__ import annotations

import sys
import time

import pytest

from balatro_bot import instance_report, jobs, supervisor
from balatro_bot.game_server import Jackdaw
from balatro_bot.instance_report import Reporter, batch_files


class Alive:
    """A process that keeps running."""
    returncode = None

    def poll(self):
        return None


@pytest.fixture
def sup(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "ROOT", tmp_path / "jobs")
    monkeypatch.setattr(supervisor, "LOG_DIR", tmp_path)
    monkeypatch.setattr(supervisor, "WINS_DIR", tmp_path / "wins")
    monkeypatch.setattr(supervisor.jobs, "kill_port_processes", lambda ports: 0)
    monkeypatch.setattr(supervisor.dashboard_client, "post_batch_start", lambda **kw: None)
    monkeypatch.setattr(supervisor.Supervisor, "start_slot", lambda self, slot: setattr(slot, "state", "running"))
    monkeypatch.setattr(supervisor, "prune_balatrobot_logs", lambda: None)
    monkeypatch.setattr(supervisor.atexit, "register", lambda fn: None)
    s = supervisor.Supervisor(n=1, games=10, decks=["BLUE"], stake="WHITE", seed=None,
                              explore_instances=1, explore_rate=0.15, server=Jackdaw(python="jackdaw-python"))
    s.dashboard_batch_id, s.session_num = 7, 42
    s._watch(s.slots[0])
    return s


def reporter(sup, slot, clock=time.time) -> Reporter:
    return Reporter(batch_files(slot.port, sup.session_num, supervisor.LOG_DIR).report, clock)


def running(slot):
    slot.state, slot.server_proc, slot.bot_proc = "running", Alive(), Alive()


@pytest.fixture
def restarts(sup, monkeypatch):
    reasons = []
    monkeypatch.setattr(supervisor.Supervisor, "restart_slot",
                        lambda self, slot, reason="unknown": reasons.append(reason))
    return reasons


# ── Counts and recent games ──

def test_status_counts_finished_games_and_lists_the_wins(sup):
    slot = sup.slots[0]
    rep = reporter(sup, slot)
    rep.game_started(0.15)
    assert sup._status("running")["recent"] == {"games": 0, "wins": 0, "lost_at": {}}
    for won, ante in ((False, 6), (False, 3), (False, 6), (True, 9)):
        rep.game_finished(won, "7KQ2M9XA" if won else "UDABG75D", ante)
    status = sup._status("running")
    assert status["recent"] == {"games": 4, "wins": 1, "lost_at": {6: 2, 3: 1}}
    assert (status["completed"], status["wins"], status["slots"][0]["progress"]) == (4, 1, "4/10")
    [win] = status["recent_wins"]
    assert (win["name"], win["seed"], win["ante"]) == (slot.name, "7KQ2M9XA", 9)


def test_summary_covers_only_the_latest_games(sup):
    rep = reporter(sup, sup.slots[0])
    for _ in range(supervisor.RECENT_WINDOW + 40):
        rep.game_finished(False, "S", 2)
    assert sup._status("running")["recent"]["games"] == supervisor.RECENT_WINDOW


def test_explore_rate_in_use_follows_each_game_start(sup, tmp_path):
    slot = sup.slots[0]
    slot.explore_active = 0.15
    jobs.write_text(slot.explore_file, "0.0")
    live = sup._status("running")["slots"][0]
    assert (live["explore_rate"], live["explore_active"]) == (0.0, 0.15)
    reporter(sup, slot).game_started(0.0)
    assert sup._status("running")["slots"][0]["explore_active"] == 0.0


def test_relaunched_bot_carries_on_from_the_reported_count(sup, monkeypatch):
    launched = []
    monkeypatch.setattr(supervisor.subprocess, "Popen", lambda cmd, **kw: launched.append(cmd))
    slot = sup.slots[0]
    reporter(sup, slot).game_finished(True, "S", 9)
    sup.launch_bot(slot)
    cmd = launched[0]
    flag = {k: cmd[cmd.index(k) + 1] for k in ("--games", "--games-offset", "--batch-number", "--explore-file")}
    assert flag == {"--games": "9", "--games-offset": "1", "--batch-number": "42",
                    "--explore-file": str(slot.explore_file)}
    sup._status("running")
    assert len(slot.recent) == 1


def test_a_slot_with_every_game_played_relaunches_with_none_left(sup):
    rep = reporter(sup, sup.slots[0])
    for _ in range(12):
        rep.game_finished(False, "S", 2)
    assert sup._remaining_games(sup.slots[0]) == 0


def test_report_and_logs_follow_the_given_batch_number(sup, tmp_path):
    """A port folder holding a log from a higher-numbered batch doesn't move this batch's files."""
    slot = sup.slots[0]
    (tmp_path / str(slot.port)).mkdir(exist_ok=True)
    (tmp_path / str(slot.port) / "game_187.log").touch()
    sup._watch(slot)
    assert slot.report.path == tmp_path / str(slot.port) / "report_042.jsonl"
    assert slot.log_file == tmp_path / str(slot.port) / "game_042.log"


# ── Soft-lock ──

def test_a_bot_reporting_itself_stuck_is_restarted(sup, restarts):
    slot = sup.slots[0]
    running(slot)
    clock = [time.time() - 200]
    rep = reporter(sup, slot, clock=lambda: clock[0])
    for _ in range(100):
        rep.action(3, "SHOP", "buy", {"card": 0})
        clock[0] += 1
    sup.check_slot(slot)
    assert restarts == ["soft-lock detected"]


def test_a_bot_making_no_move_is_restarted_after_the_stall_timeout(sup, restarts, monkeypatch):
    slot = sup.slots[0]
    running(slot)
    t = 5000.0
    reporter(sup, slot, clock=lambda: t).action(3, "SHOP", "reroll", None)
    monkeypatch.setattr(supervisor.time, "time", lambda: t + 59)
    sup.check_slot(slot)
    assert restarts == []
    monkeypatch.setattr(supervisor.time, "time", lambda: t + 60)
    sup.check_slot(slot)
    assert restarts == ["soft-lock detected"]


def test_the_live_game_waits_ten_minutes_before_calling_a_stall(sup, restarts):
    live = supervisor.Supervisor(1, 10, ["BLUE"], "WHITE", None)
    assert (live.stall_timeout, sup.stall_timeout) == (600, 60)
    assert sup._status("running")["stall_secs"] == 60
    slot = sup.slots[0]
    running(slot)
    reporter(sup, slot, clock=lambda: time.time() - 90).action(3, "SHOP", "reroll", None)
    sup.stall_timeout = live.stall_timeout
    sup.check_slot(slot)
    assert restarts == []


def test_no_stall_before_the_bot_moves_and_none_after_a_relaunch(sup, restarts, monkeypatch):
    slot = sup.slots[0]
    running(slot)
    sup.check_slot(slot)
    reporter(sup, slot, clock=lambda: time.time() - 1000).action(3, "SHOP", "reroll", None)
    monkeypatch.setattr(supervisor.subprocess, "Popen", lambda cmd, **kw: Alive())
    sup.launch_bot(slot)
    slot.server_proc = Alive()
    sup.check_slot(slot)
    assert restarts == []


def test_stream_mode_never_restarts_for_a_soft_lock(sup, restarts):
    sup.stream = True
    slot = sup.slots[0]
    sup._watch(slot)
    running(slot)
    clock = [time.time() - 1000]
    rep = Reporter(batch_files(slot.port, sup.session_num, supervisor.LOG_DIR, stream=True).report,
                   lambda: clock[0])
    for _ in range(100):
        rep.action(3, "SHOP", "buy", {"card": 0})
        clock[0] += 1
    sup.check_slot(slot)
    assert restarts == []
    assert slot.report.stuck


# ── Pause and resume ──

def test_finished_games_survive_a_pause(sup):
    slot = sup.slots[0]
    rep = reporter(sup, slot)
    rep.game_finished(False, "UDABG75D", 6)
    rep.game_finished(True, "7KQ2M9XA", 9)
    state = sup.pause_state()
    assert state["report"] is True and "recent" not in state["slots"][0]
    resumed = supervisor.Supervisor.from_pause_state(state, server=sup.server)
    resumed.setup()
    status = resumed._status("running")
    assert status["recent"] == {"games": 2, "wins": 1, "lost_at": {6: 1}}
    assert [w["seed"] for w in status["recent_wins"]] == ["7KQ2M9XA"]
    assert resumed._remaining_games(resumed.slots[0]) == 8


def test_a_batch_paused_before_reports_cannot_resume(sup, monkeypatch):
    old = sup.pause_state()
    del old["report"]
    jobs.batch().report({"phase": "paused", "resume": old})
    monkeypatch.setattr(sys, "argv", ["supervisor", "--resume"])
    with pytest.raises(SystemExit):
        supervisor.main()


def test_a_fresh_batch_numbers_itself_from_local_logs_without_the_dashboard(sup, tmp_path, monkeypatch):
    monkeypatch.setattr(supervisor.dashboard_client, "post_batch_start", lambda **kw: None)
    (tmp_path / "12399").mkdir()
    (tmp_path / "12399" / "game_186.log").touch()
    fresh = supervisor.Supervisor(n=1, games=3, decks=["BLUE"], stake="WHITE", seed=None)
    fresh.setup()
    assert fresh.session_num == instance_report.next_local_number(tmp_path) == 187
    assert fresh.slots[0].report.path.name == "report_187.jsonl"
