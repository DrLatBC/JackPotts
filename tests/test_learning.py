"""Learning runs and the stream as the control panel sees them: commands, status files,
the run list and the stream's model choice."""

import json
import os
import time

import pytest

from balatro_bot import learning

SETTINGS = {"train_run": "r1", "train_copy": "b187", "train_hours": 1.0, "train_beta": 0.1}


@pytest.fixture
def rl_files(tmp_path, monkeypatch):
    """A throwaway rl/data and bot_log for the learning-run side of the panel."""
    monkeypatch.setattr(learning, "RL_DATA", tmp_path / "rl")
    monkeypatch.setattr(learning, "TRAIN_DIR", tmp_path / "log" / "training")
    monkeypatch.setattr(learning, "STREAM_DIR", tmp_path / "log" / "stream")
    (tmp_path / "rl" / "models").mkdir(parents=True)
    (tmp_path / "log" / "training").mkdir(parents=True)
    return tmp_path


def _status(run, **fields):
    learning.train_file(run, ".json").write_text(json.dumps({"run": run, **fields}))


def _saved_run(root, name, *, seconds, hours, log=True, latest=True):
    run = root / "rl" / "ppo" / name
    run.mkdir(parents=True)
    (run / "state.json").write_text(json.dumps({"seconds": seconds, "args": {"hours": hours}}))
    if log:
        (run / "log.jsonl").write_text("{}\n")
    if latest:
        (run / "latest.pt").write_text("")
    return run


def test_train_command_passes_the_four_fields_and_the_fixed_flags():
    settings = {**SETTINGS, "train_run": "r1", "train_copy": "b187", "train_hours": 2.5, "train_beta": 0.03}
    cmd = learning.train_command(settings)
    assert cmd[0].endswith("pythonw.exe") and ".venv-rl" in cmd[0]
    assert cmd[1:12] == ["-m", "rl.ppo", "train", "--run", "r1", "--start", "b187",
                         "--hours", "2.5", "--beta", "0.03"]
    assert cmd[12:] == learning.TRAIN_FIXED
    assert "--games-per-step" in cmd and cmd[cmd.index("--fade") + 1] == "0"


SAVED_ARGS = {
    "cmd": "train", "run": "s2-steps", "slots": 64, "ticks": 256, "seed": 0, "threads": 4,
    "normal_priority": False, "hours": 1.0, "start": "b187", "plays": 4, "seeds": 32,
    "games_per_step": 1024, "target_change": 0.005, "change_sample": 4096, "resume": False,
    "beta": 0.1, "fade": 0.0, "passes": 4, "batch": 512, "lr": 0.0001, "clip": 0.2, "lam": 0.95,
    "value_coef": 0.5, "entropy_coef": 0.0, "max_grad": 0.5, "eval_every": 0.5, "eval_games": 1000,
}


def test_resume_command_replays_the_runs_own_flags():
    cmd = learning.resume_command(SAVED_ARGS)
    assert cmd[1:4] == ["-m", "rl.ppo", "train"] and cmd[-1] == "--resume"
    flags = dict(zip(cmd[4:-1:2], cmd[5:-1:2]))
    assert flags["--run"] == "s2-steps"
    assert flags["--games-per-step"] == "1024"
    assert flags["--eval-every"] == "0.5" and flags["--lr"] == "0.0001"
    assert "--cmd" not in cmd and cmd.count("--resume") == 1
    assert "--normal-priority" not in cmd


def test_resume_command_parses_back_to_the_saved_args():
    """What the panel passes on Resume reads back, through rl.ppo's own flags, as the run's args."""
    import argparse
    cmd = learning.resume_command({**SAVED_ARGS, "start": "", "normal_priority": True, "lr": 1e-05})
    assert "--normal-priority" in cmd
    ap = argparse.ArgumentParser()
    for key, value in SAVED_ARGS.items():
        if key in ("cmd", "resume", "normal_priority"):
            continue
        ap.add_argument("--" + key.replace("_", "-"), type=type(value))
    ap.add_argument("--normal-priority", action="store_true")
    ap.add_argument("--resume", action="store_true")
    back = vars(ap.parse_args(cmd[4:]))
    assert back["start"] == "" and back["lr"] == 1e-05 and back["normal_priority"] and back["resume"]
    assert back["hours"] == 1.0 and back["games_per_step"] == 1024


@pytest.mark.parametrize("name, ok", [("r1", True), ("s2-beta.03_b", True), ("", False),
                                      ("..", False), ("a/b", False), ("a b", False)])
def test_run_names(name, ok):
    assert learning.valid_run_name(name) is ok


def test_taken_and_resumable_runs(rl_files):
    _saved_run(rl_files, "half", seconds=1800, hours=1.0)
    _saved_run(rl_files, "done", seconds=3601, hours=1.0)
    _saved_run(rl_files, "nosave", seconds=10, hours=1.0, latest=False)
    assert learning.resumable("half") and learning.run_taken("half")
    assert not learning.resumable("done") and learning.run_taken("done")
    assert not learning.resumable("nosave")
    assert not learning.resumable("new") and not learning.run_taken("new")


def test_copies_lists_the_model_files(rl_files):
    for name in ("b187.pt", "b187.json", "b187_w256.pt"):
        (rl_files / "rl" / "models" / name).write_text("")
    assert learning.copies() == ["b187", "b187_w256"]


@pytest.mark.parametrize("payload, stop, phase", [
    ({"phase": "running", "pid": os.getpid()}, False, "running"),
    ({"phase": "running", "pid": os.getpid()}, True, "stopping"),
    ({"phase": "running", "pid": 999999999}, False, "failed"),
    ({"phase": "running", "pid": 999999999}, True, "failed"),
    ({"phase": "stopped", "pid": 999999999}, False, "stopped"),
    ({"phase": "finished", "pid": 999999999}, True, "finished"),
    ({"phase": "failed", "pid": 999999999, "error": "boom"}, False, "failed"),
])
def test_training_phase(rl_files, payload, stop, phase):
    _status("r", **payload)
    status = learning.read_trainings()["r"]
    assert learning.training_phase(status, stop) == phase
    if payload["phase"] == "running" and phase == "failed":
        assert status["error"]


def test_no_runs_or_a_garbled_status_is_idle(rl_files):
    assert learning.read_trainings() == {}
    assert learning.training_phase(None, False) == "idle"
    learning.train_file("r", ".json").write_text("{not json")
    assert learning.read_trainings() == {}


def test_each_run_has_its_own_status(rl_files):
    _status("a", phase="running", pid=os.getpid())
    _status("b", phase="stopped", pid=999999999)
    statuses = learning.read_trainings()
    assert {r: s["phase"] for r, s in statuses.items()} == {"a": "running", "b": "stopped"}


def _st(phase, updated):
    return {"phase": phase, "updated": updated}


@pytest.mark.parametrize("statuses, current, followed", [
    ({}, None, None),
    ({"a": _st("finished", 1), "b": _st("stopped", 2)}, None, "b"),
    ({"a": _st("running", 1), "b": _st("stopped", 2)}, None, "a"),
    # A run that plays keeps the panel while a newer one starts elsewhere.
    ({"a": _st("running", 1), "b": _st("running", 2)}, "a", "a"),
    ({"a": _st("running", 1), "b": _st("running", 2)}, None, "b"),
    # Once the followed run ends, the panel moves to one that plays...
    ({"a": _st("finished", 3), "b": _st("running", 2)}, "a", "b"),
    # ...and otherwise stays on it.
    ({"a": _st("finished", 1), "b": _st("stopped", 2)}, "a", "a"),
    ({"a": _st("finished", 1)}, "gone", "a"),
])
def test_follow(statuses, current, followed):
    assert learning.follow(statuses, current) == followed


def test_launch_training_clears_the_runs_stop_and_hides_the_window(rl_files, monkeypatch):
    seen = {}
    monkeypatch.setattr(learning.subprocess, "Popen", lambda cmd, **kw: seen.update(cmd=cmd, **kw) or "proc")
    learning.train_file("r", ".stop").touch()
    learning.train_file("other", ".stop").touch()
    learning.train_file("r", ".log").write_text("old output\n")
    assert learning.launch_training(["x"], "r", append=True) == "proc"
    assert not learning.train_file("r", ".stop").exists()
    assert learning.train_file("other", ".stop").exists()
    assert seen["creationflags"] & learning.subprocess.CREATE_NO_WINDOW
    assert learning.train_file("r", ".log").read_text() == "old output\n"
    learning.launch_training(["x"], "r")
    assert learning.train_file("r", ".log").read_text() == ""


def test_console_tail_is_the_runs_last_printed_line(rl_files):
    assert learning.console_tail("r") == ""
    assert learning.console_tail(None) == ""
    learning.train_file("r", ".log").write_text("u   1 0.03h\nTraceback\nValueError: bad\n\n")
    assert learning.console_tail("r") == "ValueError: bad"


def test_training_summary_shows_progress_and_the_last_eval():
    status = {"update": 12, "hours": 0.5, "hours total": 1.0, "games": 12288,
              "eval": {"update": 10, "win rate": 0.049, "win rate +/-": 0.007, "mean final ante": 4.02}}
    assert learning.training_summary(status) == ("update 12  ·  0.50 of 1 h  ·  12,288 games  ·  "
                                            "eval 4.9% wins, ante 4.02")




# ── The run list ──

def _log(run_dir, lines):
    (run_dir / "log.jsonl").write_text("".join(json.dumps(line) + "\n" for line in lines))


def _eval(run_dir, update, win, ante):
    summary = {"update": update, "win rate": win, "win rate +/-": 0.007, "mean final ante": ante}
    (run_dir / f"eval_u{update:05d}.json").write_text(json.dumps({"summary": summary, "games": []}))


LINE = {"update": 3, "games": 3072, "points per finished game": 3.1, "agreement with copy": 0.95, "drift": 0.01}


def test_a_run_without_a_status_reads_from_its_files(rl_files):
    run = _saved_run(rl_files, "old", seconds=1800, hours=1.0)
    _log(run, [LINE])
    _eval(run, 0, 0.049, 4.02)
    _eval(run, 3, 0.061, 4.2)
    long_ago = time.time() + learning.OUTSIDE_SECS + 60
    row = learning.run_overview("old", None, now=long_ago)
    assert row["phase"] == "stopped"
    assert (row["points per game"], row["agreement"], row["drift"]) == (3.1, 0.95, 0.01)
    assert row["eval"] == {"update": 3, "win rate": 0.061, "win rate +/-": 0.007, "mean final ante": 4.2}
    assert row["hours"] == 0.5 and row["hours total"] == 1.0


def test_a_run_that_changed_lately_without_a_status_may_still_be_playing(rl_files):
    _saved_run(rl_files, "terminal", seconds=600, hours=1.0)
    assert learning.run_overview("terminal", None)["phase"] == "outside"


def test_a_run_past_its_hours_is_finished(rl_files):
    _saved_run(rl_files, "done", seconds=3700, hours=1.0)
    assert learning.run_overview("done", None)["phase"] == "finished"


def test_a_status_wins_over_the_files_and_shows_a_pending_stop(rl_files):
    _saved_run(rl_files, "live", seconds=60, hours=1.0)
    _status("live", phase="running", pid=os.getpid(), update=9, updated=time.time())
    status = learning.read_trainings()["live"]
    assert learning.run_overview("live", status)["phase"] == "running"
    assert learning.run_overview("live", status)["update"] == 9
    learning.train_file("live", ".stop").touch()
    assert learning.run_overview("live", status)["phase"] == "stopping"


def test_a_folder_without_a_run_is_left_out(rl_files):
    (rl_files / "rl" / "ppo" / "empty").mkdir(parents=True)
    assert learning.run_overview("empty", None) is None
    assert learning.list_runs({}) == []


def test_runs_that_play_come_first_then_the_latest(rl_files):
    for name, age in (("a", 5000), ("b", 3000), ("c", 4000)):
        run = _saved_run(rl_files, name, seconds=3700, hours=1.0)
        stamp = time.time() - age
        for f in run.iterdir():
            os.utime(f, (stamp, stamp))
    _status("a", phase="running", pid=os.getpid(), updated=time.time() - 4500)
    rows = learning.list_runs(learning.read_trainings())
    assert [r["run"] for r in rows] == ["a", "b", "c"]


def test_history_reads_again_only_when_the_files_grow(rl_files):
    run = _saved_run(rl_files, "h", seconds=10, hours=1.0)
    _log(run, [LINE])
    history = learning.History()
    first = history.of("h")
    assert first["points"] == [3.1] and first["evals"] == []
    assert history.of("h") is first
    _log(run, [LINE, {**LINE, "update": 4, "points per finished game": 3.4}])
    _eval(run, 4, 0.07, 4.3)
    again = history.of("h")
    assert again["update"] == [3, 4] and again["points"] == [3.1, 3.4]
    assert again["evals"] == [(4, 0.07, 0.007, 4.3)]


# ── The stream ──

def test_stream_status_of_a_dead_launcher_reads_as_failed(rl_files):
    learning.STREAM_DIR.mkdir(parents=True)
    learning.stream_file("status.json").write_text(json.dumps({"phase": "running", "pid": 999999999}))
    status, now = learning.read_stream()
    assert status["phase"] == "failed" and status["error"] and now is None
    learning.stream_file("status.json").write_text(json.dumps({"phase": "running", "pid": os.getpid()}))
    learning.stream_file("now.json").write_text(json.dumps({"games": 3}))
    status, now = learning.read_stream()
    assert status["phase"] == "running" and now["games"] == 3


def test_the_stream_plays_runs_with_a_save_and_every_copy(rl_files):
    (rl_files / "rl" / "models" / "b187.pt").write_text("")
    run = _saved_run(rl_files, "r1", seconds=10, hours=1.0)
    for name in ("u00000.pt", "u00011.pt"):
        (run / name).write_text("")
    _saved_run(rl_files, "nosave", seconds=10, hours=1.0, latest=False)
    assert learning.model_sources() == ["run r1", "copy b187"]
    assert learning.checkpoints("run r1") == ["latest", "u00011", "u00000"]
    assert learning.checkpoints("copy b187") == ["b187"]
    assert learning.model_choice("run r1", "latest") == (run / "latest.pt", True)
    assert learning.model_choice("run r1", "u00011") == (run / "u00011.pt", False)
    assert learning.model_choice("copy b187", "b187") == (rl_files / "rl" / "models" / "b187.pt", False)


def test_stream_command_follows_only_a_runs_latest(rl_files):
    settings = {"stream_source": "run r1", "stream_checkpoint": "latest", "stream_deck": "RED",
                "stream_stake": "GOLD", "stream_delay": 0.5}
    cmd = learning.stream_command(settings)
    assert cmd[1:3] == ["-m", "balatro_bot.stream"]
    assert cmd[cmd.index("--model") + 1].endswith("latest.pt") and cmd[-1] == "--follow"
    assert cmd[cmd.index("--deck") + 1] == "RED" and cmd[cmd.index("--delay") + 1] == "0.5"
    fixed = learning.stream_command({**settings, "stream_checkpoint": "u00011"})
    assert "--follow" not in fixed


def test_launch_stream_clears_a_stale_stop(rl_files, monkeypatch):
    seen = {}
    monkeypatch.setattr(learning.subprocess, "Popen", lambda cmd, **kw: seen.update(cmd=cmd, **kw) or "proc")
    learning.STREAM_DIR.mkdir(parents=True)
    learning.stream_file("stop").touch()
    assert learning.launch_stream(["x"]) == "proc"
    assert not learning.stream_file("stop").exists()
    assert seen["creationflags"] & learning.subprocess.CREATE_NO_WINDOW


def test_a_read_that_lands_on_a_write_keeps_the_last_good_copy(tmp_path, monkeypatch):
    path = tmp_path / "status.json"
    path.write_text(json.dumps({"phase": "running"}))
    assert learning.read_json(path) == {"phase": "running"}
    real = type(path).read_text

    def busy(self, *a, **k):
        if self == path:
            raise PermissionError(13, "Access is denied")
        return real(self, *a, **k)

    monkeypatch.setattr(type(path), "read_text", busy)
    assert learning.read_json(path) == {"phase": "running"}
    monkeypatch.undo()
    path.unlink()
    assert learning.read_json(path) is None


def test_choice_of_reads_back_what_model_choice_gives(rl_files):
    for source, save in (("run r1", "latest"), ("run r1", "u00011"), ("copy b187", "b187")):
        path, _ = learning.model_choice(source, save)
        assert learning.choice_of(str(path)) == (source, save)
