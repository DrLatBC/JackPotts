"""rl.ppo's side of the control panel: per-run status, stop requests and the guard
against two processes playing one run."""

from __future__ import annotations

import json
import os
from argparse import Namespace

import pytest

from rl import ppo


@pytest.fixture
def files(tmp_path, monkeypatch):
    monkeypatch.setattr(ppo, "TRAINING", tmp_path / "training")
    monkeypatch.setattr(ppo, "PPO", tmp_path / "ppo")
    return tmp_path


def written(run):
    return json.loads(ppo.status_path(run).read_text())


def put_status(run, **fields):
    ppo.TRAINING.mkdir(parents=True, exist_ok=True)
    ppo.status_path(run).write_text(json.dumps({"run": run, **fields}))


STATE = {"updates": 7, "seconds": 1800.0, "games": 7168, "beta": 0.1}
LINE = {"points per finished game": 3.04, "agreement with copy": 0.968, "drift": 0.0032, "won": 26}
SUMMARY = {"games": 1000, "wins": 49, "win rate": 0.049, "win rate +/-": 0.0068,
           "mean final ante": 4.02, "past ante 1": 0.929, "seconds": 49.7, "update": 6, "hours": 0.4}
DEAD_PID = 999999999


def test_status_carries_progress_latest_numbers_and_the_eval(files):
    status = {"run": "r1", "phase": "running"}
    ppo.report(status, **ppo.progress(STATE, Namespace(hours=1.0)), **ppo.latest_numbers(LINE),
               **ppo.eval_numbers(SUMMARY))
    w = written("r1")
    assert w["pid"] == os.getpid() and w["updated"] > 0
    assert (w["update"], w["hours"], w["hours total"], w["games"]) == (7, 0.5, 1.0, 7168)
    assert (w["points per game"], w["agreement"], w["drift"]) == (3.04, 0.968, 0.0032)
    assert w["eval"] == {"update": 6, "win rate": 0.049, "win rate +/-": 0.0068, "mean final ante": 4.02}


def test_each_run_writes_its_own_file(files):
    ppo.report({"run": "a", "phase": "running"})
    ppo.report({"run": "b", "phase": "stopped"})
    assert (written("a")["phase"], written("b")["phase"]) == ("running", "stopped")


def test_report_merges_into_the_running_status(files):
    status = {"run": "r1", "phase": "running", "doing": "playing"}
    ppo.report(status, **ppo.eval_numbers(SUMMARY))
    ppo.report(status, phase="stopped", doing="")
    assert written("r1")["phase"] == "stopped" and written("r1")["eval"]["update"] == 6


def test_a_stop_request_reaches_only_its_run_and_is_seen_once(files):
    ppo.TRAINING.mkdir(parents=True)
    ppo.stop_path("other").touch()
    assert not ppo.stop_requested("r1")
    ppo.stop_path("r1").touch()
    assert ppo.stop_requested("r1")
    assert not ppo.stop_path("r1").exists() and ppo.stop_path("other").exists()
    assert not ppo.stop_requested("r1")


def test_last_eval_is_the_newest_file(tmp_path):
    assert ppo.last_eval(tmp_path) == {}
    for update in (0, 12, 3):
        (tmp_path / f"eval_u{update:05d}.json").write_text(json.dumps({"summary": {**SUMMARY, "update": update}}))
    assert ppo.last_eval(tmp_path)["eval"]["update"] == 12


def test_a_resumed_run_starts_with_its_last_updates_numbers(tmp_path):
    log = tmp_path / "log.jsonl"
    assert ppo.last_numbers(log) == {}
    log.write_text(json.dumps({**LINE, "drift": 0.001}) + "\n" + json.dumps(LINE) + "\n")
    assert ppo.last_numbers(log) == {"points per game": 3.04, "agreement": 0.968, "drift": 0.0032}


def test_claim_refuses_a_run_another_live_process_plays(files):
    other = os.getppid()
    assert ppo.pid_alive(other)
    put_status("r1", phase="running", pid=other, update=12)
    with pytest.raises(SystemExit, match=f"process {other}"):
        ppo.claim("r1")
    assert written("r1") == {"run": "r1", "phase": "running", "pid": other, "update": 12}


@pytest.mark.parametrize("previous", [
    {"phase": "running", "pid": DEAD_PID},
    {"phase": "stopped", "pid": os.getppid()},
    {"phase": "finished", "pid": DEAD_PID},
])
def test_claim_takes_a_run_nobody_plays(files, previous):
    put_status("r1", **previous)
    ppo.stop_path("r1").touch()
    status = ppo.claim("r1")
    assert (status["phase"], status["pid"]) == ("running", os.getpid())
    assert written("r1")["doing"] == "starting"
    assert not ppo.stop_path("r1").exists()


def test_failure_keeps_this_processs_numbers(files):
    ppo.report({"run": "r1", "phase": "running", "update": 4})
    ppo.report_failure("r1", "RuntimeError: out of memory")
    w = written("r1")
    assert (w["phase"], w["update"], w["error"]) == ("failed", 4, "RuntimeError: out of memory")


def test_failure_leaves_a_status_this_process_never_claimed(files):
    put_status("r1", phase="finished", pid=DEAD_PID, update=99)
    ppo.report_failure("r1", "no start.pt")
    assert written("r1") == {"run": "r1", "phase": "finished", "pid": DEAD_PID, "update": 99}


def _main(monkeypatch, *argv):
    monkeypatch.setattr("sys.argv", ["ppo", "train", "--hours", "1", "--normal-priority", *argv])
    with pytest.raises(SystemExit) as e:
        ppo.main()
    return str(e.value.code)


def test_main_refusing_a_taken_name_leaves_its_status(files, monkeypatch):
    (ppo.PPO / "taken").mkdir(parents=True)
    (ppo.PPO / "taken" / "log.jsonl").write_text("{}\n")
    put_status("taken", phase="finished", pid=DEAD_PID, update=30)
    assert "log.jsonl exists" in _main(monkeypatch, "--run", "taken")
    assert written("taken")["phase"] == "finished"


def test_main_refusing_a_run_played_elsewhere_leaves_its_status(files, monkeypatch):
    put_status("busy", phase="running", pid=os.getppid(), update=5)
    assert "already being played" in _main(monkeypatch, "--run", "busy", "--resume")
    assert written("busy") == {"run": "busy", "phase": "running", "pid": os.getppid(), "update": 5}


def test_main_marks_a_claimed_run_failed(files, monkeypatch):
    def no_start(args, out):
        raise SystemExit("no start.pt: pass --start <copy> or run fit-value first")
    monkeypatch.setattr(ppo, "starting_point", no_start)
    assert "no start.pt" in _main(monkeypatch, "--run", "fresh")
    w = written("fresh")
    assert (w["phase"], w["pid"]) == ("failed", os.getpid()) and "no start.pt" in w["error"]


@pytest.mark.skipif(os.name != "nt", reason="Windows priority classes")
def test_a_run_really_drops_to_below_normal_priority():
    import subprocess
    import sys
    from pathlib import Path

    probe = ("import ctypes; from rl.ppo import below_normal_priority; below_normal_priority(); "
             "k = ctypes.WinDLL('kernel32'); k.GetCurrentProcess.restype = ctypes.c_void_p; "
             "k.GetPriorityClass.argtypes = (ctypes.c_void_p,); print(hex(k.GetPriorityClass(k.GetCurrentProcess())))")
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True,
                         cwd=Path(ppo.__file__).resolve().parents[1])
    assert out.stdout.strip() == hex(ppo.BELOW_NORMAL) and not out.stderr
