"""The panel's upload of learning runs and the stream to the web dashboard."""

import json

import pytest

from balatro_bot import jobs, learning, learning_sync
from balatro_bot.learning_sync import Uploader


class FakeDashboard:
    """The dashboard's two ingest endpoints: stores what arrives (a line twice is kept
    once) and replies with what it holds. ``down`` makes every send fail."""

    def __init__(self):
        self.updates: dict[str, dict[int, dict]] = {}
        self.evals: dict[str, dict[tuple, dict]] = {}
        self.overviews: dict[str, dict] = {}
        self.games: dict[tuple, dict] = {}
        self.stream = None
        self.sends = []
        self.down = False

    def __call__(self, path, payload):
        self.sends.append((path, payload))
        if self.down:
            return None
        if path == "/api/ingest/stream":
            self.stream = (payload.get("status"), payload.get("now"))
            for g in payload.get("games", []):
                self.games.setdefault((g["ended"], g["seed"]), g)
            return {"last_ended": max((k[0] for k in self.games), default=None)}
        name = path.rsplit("/", 1)[1]
        self.overviews[name] = payload["overview"]
        ups = self.updates.setdefault(name, {})
        for u in payload.get("updates", []):
            ups.setdefault(u["update"], u)
        evs = self.evals.setdefault(name, {})
        for e in payload.get("evals", []):
            evs[(e["kind"], e["summary"]["update"])] = e["summary"]
        return {"last_update": max(ups, default=-1),
                "evals": {k: sorted(u for kind, u in evs if kind == k) for k in ("eval", "check")}}


@pytest.fixture
def files(tmp_path, monkeypatch):
    monkeypatch.setattr(learning, "RL_DATA", tmp_path / "rl")
    monkeypatch.setattr(jobs, "ROOT", tmp_path / "log")
    return tmp_path


def _run(root, name, updates, evals=(), checks=()):
    folder = root / "rl" / "ppo" / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "state.json").write_text(json.dumps({"seconds": 3600, "updates": len(updates),
                                                   "args": {"hours": 1.0, "start": "b187"}}))
    (folder / "log.jsonl").write_text("".join(
        json.dumps({"update": u, "points per finished game": u / 10, "agreement with copy": 0.9,
                    "drift": 0.1}) + "\n" for u in updates))
    for kind, ups in (("eval", evals), ("check", checks)):
        for u in ups:
            games = [{"seed": f"S{i}", "won": i < 2, "ante": 9 if i < 2 else 3} for i in range(10)]
            summary = {"games": 10, "wins": 2, "win rate": 0.2, "win rate +/-": 0.1,
                       "mean final ante": 4.2, "update": u}
            (folder / f"{kind}_u{u:05d}.json").write_text(json.dumps({"summary": summary, "games": games}))
    return folder


def _append(folder, *updates):
    with (folder / "log.jsonl").open("a") as f:
        for u in updates:
            f.write(json.dumps({"update": u, "points per finished game": 1.0}) + "\n")


def test_a_run_goes_whole_the_first_time(files):
    _run(files, "r1", range(5), evals=[0, 4], checks=[4])
    dash = FakeDashboard()
    Uploader(dash).sync()
    assert sorted(dash.updates["r1"]) == [0, 1, 2, 3, 4]
    assert set(dash.evals["r1"]) == {("eval", 0), ("eval", 4), ("check", 4)}
    o = dash.overviews["r1"]
    assert o["run"] == "r1" and o["phase"] == "finished" and o["copy"] == "b187"


def test_later_sends_carry_only_whats_new(files):
    folder = _run(files, "r1", range(3), evals=[0])
    dash = FakeDashboard()
    up = Uploader(dash)
    up.sync()
    _append(folder, 3, 4)
    dash.sends.clear()
    up.sync()
    (sent,) = [p for path, p in dash.sends if p.get("updates")]
    assert [u["update"] for u in sent["updates"]] == [3, 4]
    assert sent["evals"] == []
    assert sorted(dash.updates["r1"]) == [0, 1, 2, 3, 4]


def test_an_unchanged_run_is_not_sent_again(files):
    _run(files, "r1", range(3))
    dash = FakeDashboard()
    up = Uploader(dash)
    up.sync()
    dash.sends.clear()
    up.sync()
    assert dash.sends == []


def test_a_failed_send_is_made_up_by_the_next(files):
    folder = _run(files, "r1", range(2))
    dash = FakeDashboard()
    up = Uploader(dash)
    up.sync()
    _append(folder, 2, 3)
    dash.down = True
    up.sync()
    dash.down = False
    up.sync()
    assert sorted(dash.updates["r1"]) == [0, 1, 2, 3]


def test_a_new_panel_starts_from_what_the_dashboard_holds(files):
    folder = _run(files, "r1", range(4))
    dash = FakeDashboard()
    Uploader(dash).sync()
    _append(folder, 4)
    dash.sends.clear()
    Uploader(dash).sync()  # the panel reopened: it knows nothing until the dashboard replies
    sent = [p for _, p in dash.sends if p.get("updates")]
    assert [u["update"] for p in sent for u in p["updates"]] == [4]


def test_long_logs_go_in_chunks(files, monkeypatch):
    monkeypatch.setattr(learning_sync, "CHUNK", 3)
    _run(files, "r1", range(8))
    dash = FakeDashboard()
    Uploader(dash).sync()
    sizes = [len(p["updates"]) for _, p in dash.sends if p.get("updates")]
    assert sizes == [3, 3, 2] and sorted(dash.updates["r1"]) == list(range(8))


def test_comparisons_go_worded_as_the_panel_words_them(files):
    _run(files, "r1", range(2), evals=[0, 1])
    dash = FakeDashboard()
    Uploader(dash).sync()
    o = dash.overviews["r1"]
    assert o["vs start"]["update"] == 1
    assert o["vs start text"].startswith("20.00% wins against 20.00%")


def test_one_broken_run_does_not_stop_the_others(files, monkeypatch):
    _run(files, "a", range(2))
    _run(files, "b", range(2))
    dash = FakeDashboard()
    up = Uploader(dash)
    real = up.sync_run
    monkeypatch.setattr(up, "sync_run", lambda row: (_ for _ in ()).throw(OSError("locked"))
                        if row["run"] == "a" else real(row))
    up.sync()
    assert "b" in dash.updates and "a" not in dash.updates


def _stream(root, games, phase="running"):
    folder = jobs.stream().folder
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "status.json").write_text(json.dumps({"phase": phase, "pid": 0}))
    (folder / "now.json").write_text(json.dumps({"checkpoint": "latest (update 3)", "game": {"seed": "X"},
                                                 "games": len(games), "recent": games[:2]}))
    (folder / "games.jsonl").write_text("".join(json.dumps(g) + "\n" for g in games))
    return folder


def _game(i):
    return {"seed": f"G{i}", "outcome": "lost", "ante": 2, "ended": 1000.0 + i, "checkpoint": "latest"}


def test_stream_games_go_once_and_only_new_ones_after(files):
    folder = _stream(files, [_game(0), _game(1)], phase="stopped")
    dash = FakeDashboard()
    up = Uploader(dash)
    up.sync()
    assert sorted(dash.games) == [(1000.0, "G0"), (1001.0, "G1")]
    with (folder / "games.jsonl").open("a") as f:
        f.write(json.dumps(_game(2)) + "\n")
    dash.sends.clear()
    up.sync()
    (sent,) = [p for _, p in dash.sends if p.get("games")]
    assert [g["seed"] for g in sent["games"]] == ["G2"]


def test_stream_sends_leave_out_the_recent_list(files):
    _stream(files, [_game(0)], phase="stopped")
    dash = FakeDashboard()
    Uploader(dash).sync()
    status, now = dash.stream
    assert status["phase"] == "stopped" and "recent" not in now and now["checkpoint"] == "latest (update 3)"


def test_a_dead_stream_launcher_goes_as_failed(files):
    _stream(files, [], phase="running")  # pid 0 is never alive
    dash = FakeDashboard()
    Uploader(dash).sync()
    assert dash.stream[0]["phase"] == "failed"


def test_nothing_starts_without_a_dashboard(monkeypatch):
    monkeypatch.delenv("JACKPOTTS_URL", raising=False)
    assert Uploader(FakeDashboard()).start() is None
