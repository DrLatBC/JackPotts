"""Learning runs and the stream as the control panel sees them: commands, status files,
the run list and the stream's model choice."""

import json
import os
import time

import pytest

from balatro_bot import jobs, learning

SETTINGS = {"train_run": "r1", "train_copy": "b187", "train_hours": 1.0, "train_beta": 0.1,
            "train_method": "SAME SEED", "train_bonus": 8.0, "train_deck": "BLUE", "train_stake": "WHITE"}


@pytest.fixture
def rl_files(tmp_path, monkeypatch):
    """A throwaway rl/data and bot_log for the learning-run side of the panel."""
    monkeypatch.setattr(learning, "RL_DATA", tmp_path / "rl")
    monkeypatch.setattr(jobs, "ROOT", tmp_path / "log")
    (tmp_path / "rl" / "models").mkdir(parents=True)
    return tmp_path


def _status(run, **fields):
    path = jobs.learning_run(run).file("status.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"run": run, **fields}))


def _saved_run(root, name, *, seconds, hours, log=True, latest=True):
    run = root / "rl" / "ppo" / name
    run.mkdir(parents=True)
    (run / "state.json").write_text(json.dumps({"seconds": seconds, "args": {"hours": hours}}))
    if log:
        (run / "log.jsonl").write_text("{}\n")
    if latest:
        (run / "latest.pt").write_text("")
    return run


def test_train_command_passes_the_fields_and_the_fixed_flags():
    settings = {**SETTINGS, "train_run": "r1", "train_copy": "b187", "train_hours": 2.5, "train_beta": 0.03}
    cmd = learning.train_command(settings)
    assert cmd[0].endswith("pythonw.exe") and ".venv-rl" in cmd[0]
    assert cmd[1:18] == ["-m", "rl.ppo", "train", "--run", "r1", "--start", "b187",
                         "--hours", "2.5", "--beta", "0.03", "--win-bonus", "8.0",
                         "--deck", "BLUE", "--stake", "WHITE"]
    assert cmd[18:] == learning.TRAIN_FIXED + learning.METHOD_FIXED["SAME SEED"]
    assert "--games-per-step" in cmd and cmd[cmd.index("--fade") + 1] == "0"
    assert "--branch-rate" not in cmd


def test_a_branch_run_gets_the_branch_flags_and_its_deck_stake_and_bonus():
    cmd = learning.train_command({**SETTINGS, "train_method": "BRANCHES", "train_bonus": 16.0,
                                  "train_deck": "RED", "train_stake": "BLACK"})
    flags = dict(zip(cmd[4::2], cmd[5::2]))
    assert flags["--branch-rate"] == "0.05" and flags["--decisions-per-step"] == "2000"
    assert flags["--fade"] == "0"
    assert (flags["--win-bonus"], flags["--deck"], flags["--stake"]) == ("16.0", "RED", "BLACK")
    assert "--plays" not in cmd and "--games-per-step" not in cmd


def test_a_branch_run_takes_its_decisions_per_step_from_the_panel():
    cmd = learning.train_command({**SETTINGS, "train_method": "BRANCHES", "train_decisions": 6000})
    assert cmd[cmd.index("--decisions-per-step") + 1] == "6000"
    assert cmd.count("--decisions-per-step") == 1
    same_seed = learning.train_command({**SETTINGS, "train_decisions": 6000})
    assert "--decisions-per-step" not in same_seed


def test_a_runs_settings_come_from_its_saved_args_with_the_old_defaults():
    got = learning.run_settings({"branch_rate": 0.05, "deck": "RED", "stake": "GOLD", "win_bonus": 16.0,
                                 "decisions_per_step": 6000})
    assert got == {"method": "BRANCHES", "deck": "RED", "stake": "GOLD", "win bonus": 16.0, "decisions": 6000}
    got = learning.run_settings({"branch_rate": 0.05})
    assert got["decisions"] == 2000
    got = learning.run_settings({"plays": 4, "branch_rate": 0.0})
    assert got == {"method": "SAME SEED", "deck": "BLUE", "stake": "WHITE", "win bonus": 8.0, "decisions": None}


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
    ap.add_argument("--final-check", type=int)
    back = vars(ap.parse_args(cmd[4:]))
    assert back["start"] == "" and back["lr"] == 1e-05 and back["normal_priority"] and back["resume"]
    assert back["hours"] == 1.0 and back["games_per_step"] == 1024


def test_resume_can_give_the_run_new_hours_and_adds_fixed_flags_it_lacks():
    cmd = learning.resume_command({**SAVED_ARGS, "eval_games": 200}, hours=9.0)
    flags = dict(zip(cmd[4:-1:2], cmd[5:-1:2]))
    assert flags["--hours"] == "9.0"
    assert flags["--final-check"] == "5000"  # the run was started without it
    assert flags["--eval-games"] == "200"  # its own value wins over the panel's
    assert cmd.count("--final-check") == 1 and cmd[-1] == "--resume"


def test_a_finished_run_resumes_when_given_more_hours(rl_files):
    _saved_run(rl_files, "done", seconds=3601, hours=1.0)
    assert not learning.resumable("done")
    assert not learning.resumable("done", 1.0)
    assert learning.resumable("done", 9.0)
    assert learning.hours_done("done") == pytest.approx(3601 / 3600)


def test_a_run_starts_from_a_copy_or_a_runs_update_save(rl_files):
    (rl_files / "rl" / "models" / "b187.pt").write_text("")
    run = _saved_run(rl_files, "r1", seconds=10, hours=1.0)
    for name in ("u00000.pt", "u00019.pt"):
        (run / name).write_text("")
    _saved_run(rl_files, "latest-only", seconds=10, hours=1.0)
    assert learning.start_sources() == ["copy b187", "run r1"]
    assert learning.start_saves("run r1") == ["u00019", "u00000"]  # never latest
    assert learning.start_saves("copy b187") == ["b187"]
    assert learning.start_arg("copy b187", "b187") == "b187"
    for source, save in (("copy b187", "b187"), ("run r1", "u00019")):
        assert learning.start_choice(learning.start_arg(source, save)) == (source, save)


def test_a_start_under_the_repository_is_passed_as_a_relative_path(monkeypatch):
    monkeypatch.setattr(learning, "RL_DATA", learning.REPO / "rl" / "data")
    assert learning.start_arg("run s2-steps", "u00019") == "rl/data/ppo/s2-steps/u00019.pt"
    assert learning.start_choice("rl/data/ppo/s2-steps/u00019.pt") == ("run s2-steps", "u00019")


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
    if stop:
        jobs.learning_run("r").ask("stop")
    status = learning.read_trainings()["r"]
    assert status["phase"] == phase
    if payload["phase"] == "running" and phase == "failed":
        assert status["error"]


def test_no_runs_or_a_garbled_status_is_idle(rl_files):
    assert learning.read_trainings() == {}
    _status("r")
    jobs.learning_run("r").file("status.json").write_text("{not json")
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


def test_training_summary_shows_progress_and_the_last_eval():
    status = {"update": 12, "hours": 0.5, "hours total": 1.0, "games": 12288,
              "eval": {"update": 10, "win rate": 0.049, "win rate +/-": 0.007, "mean final ante": 4.02}}
    assert learning.training_summary(status) == ("update 12  ·  0.50 of 1 h  ·  12,288 games  ·  "
                                            "eval 4.9% wins, ante 4.02")




# ── The run list ──

def _log(run_dir, lines):
    (run_dir / "log.jsonl").write_text("".join(json.dumps(line) + "\n" for line in lines))


def _eval(run_dir, update, win, ante, games=()):
    summary = {"update": update, "win rate": win, "win rate +/-": 0.007, "mean final ante": ante}
    (run_dir / f"eval_u{update:05d}.json").write_text(json.dumps({"summary": summary, "games": list(games)}))


def _games(antes):
    return [{"seed": f"RLE-{i:06d}", "won": a == 9, "ante": a} for i, a in enumerate(antes, 1)]


def test_the_newest_eval_is_compared_with_update_0_on_the_same_seeds(rl_files):
    run = _saved_run(rl_files, "r1", seconds=10, hours=1.0)
    _eval(run, 0, 0.0, 3.0, _games([3, 3, 3, 3]))
    assert learning.eval_vs_start("r1") is None
    _eval(run, 6, 0.25, 4.5, _games([3, 9, 3, 3]))
    _eval(run, 11, 0.5, 5.0, _games([9, 9, 3, 3]))
    vs = learning.eval_vs_start("r1")
    assert vs["update"] == 11 and vs["seeds"] == 4
    assert vs["win gain"] == 0.5 and vs["ante gain"] == 3.0
    assert vs["won only here"] == 2 and vs["won only by base"] == 0
    assert learning.run_overview("r1", None)["vs start"]["update"] == 11


def test_a_runs_newest_check_shows_in_its_row_and_summary(rl_files):
    run = _saved_run(rl_files, "r1", seconds=3601, hours=1.0)
    for update, gain in ((10, 0.01), (19, 0.0444)):
        summary = {"update": update, "vs start": {"seeds": 5000, "win rate": 0.0894, "base win rate": 0.045,
                                                  "win gain": gain, "win gain +/-": 0.0043}}
        (run / f"check_u{update:05d}.json").write_text(json.dumps({"summary": summary, "games": []}))
    row = learning.run_overview("r1", None)
    assert row["check"]["update"] == 19 and row["check"]["win gain"] == 0.0444
    text = learning.training_summary({"update": 19, "hours": 1, "hours total": 1, "games": 10, **row})
    assert "5,000-seed check 8.9% wins (+4.4 ± 0.4 on the start)" in text


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
    jobs.learning_run("live").ask("stop")
    assert learning.run_overview("live", learning.read_trainings()["live"])["phase"] == "stopping"


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
    job = jobs.stream()
    job.folder.mkdir(parents=True)
    job.file("status.json").write_text(json.dumps({"phase": "running", "pid": 999999999}))
    status, now = learning.read_stream()
    assert status["phase"] == "failed" and status["error"] and now is None
    job.file("status.json").write_text(json.dumps({"phase": "running", "pid": os.getpid()}))
    job.file("now.json").write_text(json.dumps({"games": 3}))
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


def test_choice_of_reads_back_what_model_choice_gives(rl_files):
    for source, save in (("run r1", "latest"), ("run r1", "u00011"), ("copy b187", "b187")):
        path, _ = learning.model_choice(source, save)
        assert learning.choice_of(str(path)) == (source, save)
