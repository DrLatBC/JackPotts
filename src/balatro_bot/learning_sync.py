"""Sends the web dashboard the learning runs and the model stream, from the files the
control panel reads: each run's overview, its log.jsonl lines, its evaluation and
final-check summaries, and the stream's status, game and finished games.

Each reply says what the dashboard already holds (a run's last update and evaluations,
the stream's newest game), so a send carries only what is past that, and a failed send
is made up by the next. The panel runs ``Uploader.start`` once; it does nothing while
``JACKPOTTS_URL`` is unset.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from balatro_bot import dashboard_client, learning
from balatro_bot.eval_compare import describe

logger = logging.getLogger(__name__)

INTERVAL = 30.0
CHUNK = 500  # log lines or stream games per send


def _lines(path: Path) -> list[dict]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue  # a line still being written
    return out


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return -1


def overview(row: dict) -> dict:
    """The panel's row for a run, with its comparisons as the panel words them."""
    out = dict(row)
    for key in ("check", "vs start"):
        if out.get(key):
            out[f"{key} text"] = describe(out[key])
    return out


def summaries(name: str) -> list[dict]:
    """The run's evaluation and final-check summaries, oldest first."""
    folder = learning.run_dir(name)
    out = []
    for kind, pattern in (("eval", "eval_u*.json"), ("check", "check_u*.json")):
        for path in sorted(folder.glob(pattern)):
            s = learning._eval_summary(path)
            if s is not None and isinstance(s.get("update"), int):
                out.append({"kind": kind, "summary": s})
    return out


class Uploader:
    def __init__(self, post=None) -> None:
        self.post = post or dashboard_client.post_learning
        self.held: dict[str, dict] = {}  # run -> the dashboard's last reply
        self.seen: dict[str, tuple] = {}  # run -> (overview, log size, summaries) last sent
        self.stream_last: float | None = None
        self.stream_known = False
        self.stream_seen: tuple | None = None
        self._stop = threading.Event()

    # -- runs -----------------------------------------------------------------

    def sync_run(self, row: dict) -> None:
        name = row["run"]
        view = overview(row)
        log = learning.run_dir(name) / "log.jsonl"
        evals = summaries(name)
        mark = (json.dumps(view, sort_keys=True, default=str), _size(log), json.dumps(evals, sort_keys=True))
        if self.seen.get(name) == mark:
            return
        held = self.held.get(name)
        if held is None:
            held = self.post(f"/api/ingest/training/{name}", {"overview": view})
            if held is None:
                return
        updates = [u for u in _lines(log) if isinstance(u.get("update"), int) and u["update"] > held["last_update"]]
        known = {(k, u) for k, us in held["evals"].items() for u in us}
        # A final check is replaced in place once it finishes, so checks always go.
        new = [e for e in evals if e["kind"] == "check" or (e["kind"], e["summary"]["update"]) not in known]
        while True:
            reply = self.post(f"/api/ingest/training/{name}",
                              {"overview": view, "updates": updates[:CHUNK], "evals": new})
            if reply is None:
                self.held.pop(name, None)
                return
            self.held[name], updates, new = reply, updates[CHUNK:], []
            if not updates:
                break
        self.seen[name] = mark

    # -- the stream -----------------------------------------------------------

    def sync_stream(self) -> None:
        status, now = learning.read_stream()
        if status is None and now is None:
            return
        now = {k: v for k, v in (now or {}).items() if k != "recent"}  # the games go one by one
        games_path = learning.stream_file("games.jsonl")
        mark = (json.dumps([status, now], sort_keys=True, default=str), _size(games_path))
        if mark == self.stream_seen:
            return
        if not self.stream_known:
            reply = self.post("/api/ingest/stream", {"status": status, "now": now})
            if reply is None:
                return
            self.stream_last, self.stream_known = reply.get("last_ended"), True
        games = [g for g in _lines(games_path)
                 if g.get("ended") is not None and (self.stream_last is None or g["ended"] > self.stream_last)]
        while True:
            reply = self.post("/api/ingest/stream", {"status": status, "now": now, "games": games[:CHUNK]})
            if reply is None:
                self.stream_known = False
                return
            self.stream_last, games = reply.get("last_ended"), games[CHUNK:]
            if not games:
                break
        self.stream_seen = mark

    # -- the loop -------------------------------------------------------------

    def sync(self) -> None:
        for row in learning.list_runs(learning.read_trainings()):
            try:
                self.sync_run(row)
            except Exception:  # one run's files must not stop the others
                logger.warning("Dashboard sync of run %s failed", row.get("run"), exc_info=True)
        try:
            self.sync_stream()
        except Exception:
            logger.warning("Dashboard sync of the stream failed", exc_info=True)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.sync()
            self._stop.wait(INTERVAL)

    def start(self) -> threading.Thread | None:
        """The upload thread, or None while the dashboard isn't configured."""
        if not dashboard_client._url():
            return None
        thread = threading.Thread(target=self._loop, name="dashboard-sync", daemon=True)
        thread.start()
        return thread

    def stop(self) -> None:
        self._stop.set()
