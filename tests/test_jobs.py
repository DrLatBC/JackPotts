"""Jobs: status, liveness, claims, requests, commands and spawn, on real files in a temp root."""

import json
import os
import subprocess
import sys

import pytest

from balatro_bot import jobs


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "ROOT", tmp_path)
    return tmp_path


def dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def write_status(job, **status):
    job.folder.mkdir(parents=True, exist_ok=True)
    job.file("status.json").write_text(json.dumps(status))


def test_layout(root):
    assert jobs.batch().folder == root / "batch"
    assert jobs.learning_run("r1").folder == root / "training" / "r1"
    assert jobs.stream().folder == root / "model_stream"


def test_no_status_reads_none(root):
    assert jobs.batch().status() is None
    assert not jobs.batch().live()


def test_report_round_trip(root):
    job = jobs.learning_run("r1")
    status = {"phase": "running"}
    job.report(status, update=3)
    read = job.status()
    assert read["phase"] == "running" and read["update"] == 3
    assert read["pid"] == os.getpid() and read["job"] == "learning run"
    assert job.live()


@pytest.mark.parametrize("phase", jobs.LIVE)
def test_live_phase_with_dead_pid_reads_failed(root, phase):
    job = jobs.stream()
    write_status(job, phase=phase, pid=dead_pid())
    read = job.status()
    assert read["phase"] == "failed" and read["error"] == jobs.DIED
    assert not job.live()


@pytest.mark.parametrize("phase", ["stopped", "finished", "paused"])
def test_ended_phase_with_dead_pid_stays(root, phase):
    job = jobs.batch()
    write_status(job, phase=phase, pid=dead_pid())
    assert job.status()["phase"] == phase


@pytest.mark.parametrize("request_, shown", [("stop", "stopping"), ("pause", "pausing")])
def test_running_with_a_request_reads_as_its_phase(root, request_, shown):
    job = jobs.batch()
    job.report({"phase": "running"})
    job.ask(request_)
    assert job.status()["phase"] == shown


def test_claim_refuses_a_live_holder(root):
    job = jobs.learning_run("r1")
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        write_status(job, phase="running", pid=holder.pid)
        with pytest.raises(jobs.Busy) as e:
            job.claim()
        assert e.value.pid == holder.pid
        assert job.status()["pid"] == holder.pid
    finally:
        holder.kill()


def test_claim_over_a_dead_holder_clears_requests(root):
    job = jobs.stream()
    write_status(job, phase="running", pid=dead_pid())
    job.ask("stop")
    status = job.claim(model="m")
    assert status["phase"] == "starting" and status["model"] == "m"
    assert job.status()["pid"] == os.getpid()
    assert not job.asked("stop")


def test_fail_only_marks_the_holder(root):
    job = jobs.learning_run("r1")
    write_status(job, phase="stopped", pid=dead_pid())
    job.fail("boom")
    assert job.status()["phase"] == "stopped"
    job.claim()
    job.fail("boom")
    assert job.status()["phase"] == "failed" and job.status()["error"] == "boom"


def test_requests_are_seen_until_cleared(root):
    job = jobs.stream()
    job.ask("stop")
    assert job.asked("stop") and job.asked("stop")
    assert not job.asked("pause")
    job.clear()
    assert not job.asked("stop")


def test_commands_come_back_in_order_once(root):
    job = jobs.batch()
    for n in range(3):
        job.send({"op": "add", "n": n})
    (job.file("commands") / "0.json").write_text("{bad")
    assert job.take() == [{}, {"op": "add", "n": 0}, {"op": "add", "n": 1}, {"op": "add", "n": 2}]
    assert job.take() == []


def test_write_retries_while_the_file_is_held(root, monkeypatch):
    calls = []
    real = os.replace

    def held(src, dst):
        calls.append(dst)
        if len(calls) < 3:
            raise PermissionError
        real(src, dst)

    monkeypatch.setattr(jobs.os, "replace", held)
    monkeypatch.setattr(jobs.time, "sleep", lambda s: None)
    job = jobs.batch()
    job.report({"phase": "running"})
    assert len(calls) == 3 and job.status()["phase"] == "running"


def test_a_write_that_never_gets_through_is_skipped(root, monkeypatch):
    def held(src, dst):
        raise PermissionError

    monkeypatch.setattr(jobs.os, "replace", held)
    monkeypatch.setattr(jobs.time, "sleep", lambda s: None)
    jobs.batch().report({"phase": "running"})
    assert jobs.batch().status() is None


def test_read_keeps_the_last_good_copy(root):
    job = jobs.batch()
    job.report({"phase": "running"})
    assert job.status()["phase"] == "running"
    job.file("status.json").write_text("{half")
    assert job.status()["phase"] == "running"
    job.file("status.json").unlink()
    assert job.status() is None


def test_spawn_prints_into_the_console_log(root):
    job = jobs.learning_run("r1")
    job.ask("stop")
    job.spawn([sys.executable, "-c", "print('first')"]).wait()
    assert not job.asked("stop")
    job.spawn([sys.executable, "-c", "print('second')"], append=True).wait()
    assert job.file("console.log").read_text().split() == ["first", "second"]
    assert job.console_tail() == "second"


def test_learning_runs_lists_runs_with_a_status(root):
    jobs.learning_run("b").report({"phase": "stopped"})
    jobs.learning_run("a").report({"phase": "stopped"})
    (root / "training" / "empty").mkdir()
    assert [j.name for j in jobs.learning_runs()] == ["a", "b"]
