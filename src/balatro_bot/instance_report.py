"""The instance report: what an instance's bot is doing, written for the supervisor.

The bot writes it through a ``Reporter``, the supervisor reads it through a
``Watcher``. One JSON object per line in ``bot_log/<port>/report_NNN.jsonl``,
appended across relaunches within a batch. This module also owns the batch
number rule and the name of every per-batch file, so the bot, the supervisor,
log rotation and ``stats`` agree on them. Standard library only.
"""

from __future__ import annotations

import json
import re
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Union

from balatro_bot.config import LOG_DIR

HEARTBEAT_EVERY = 5.0  # seconds between heartbeats at most
CYCLE_WINDOW = 40      # recent moves the cycle rule looks at
CYCLE_MIN_MOVES = 20   # moves seen before a cycle can be flagged
CYCLE_MIN_REPS = 5     # full repetitions that make a cycle
CYCLE_MAX_PERIOD = 4   # longest repeating pattern checked
CYCLE_HOLD = 60.0      # seconds a cycle lasts before the bot reports itself stuck

Clock = Callable[[], float]


# ---------------------------------------------------------------------------
# File names
# ---------------------------------------------------------------------------

_GAME_LOG = re.compile(r"game_(\d+)\.log$")


@dataclass(frozen=True)
class BatchFiles:
    """Every file one port writes for one batch."""

    game_log: Path
    scoring_log: Path
    wins_log: Path
    replay: Path
    report: Path


def batch_files(port: int, number: int, root: Path = LOG_DIR, *, stream: bool = False) -> BatchFiles:
    """The files *port* writes in batch *number*; ``stream`` puts them in ``stream/``."""
    folder = root / ("stream" if stream else str(port))
    n = f"{number:03d}"
    return BatchFiles(
        game_log=folder / f"game_{n}.log",
        scoring_log=folder / f"scoring_{n}.log",
        wins_log=root / "wins" / f"wins_{port}_{n}.log",
        replay=folder / f"replay_{n}.jsonl.gz",
        report=folder / f"report_{n}.jsonl",
    )


def batch_numbers(root: Path = LOG_DIR) -> set[int]:
    """Batch numbers with a game log in any folder under *root*."""
    return {int(m.group(1)) for p in root.glob("*/game_*.log") if (m := _GAME_LOG.match(p.name))}


def next_local_number(root: Path = LOG_DIR) -> int:
    """The batch number to use when the dashboard gives none: one past the highest local one."""
    return max(batch_numbers(root), default=0) + 1


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GameStarted:
    t: float
    explore: float


@dataclass(frozen=True)
class GameFinished:
    t: float
    won: bool
    seed: str
    ante: int


@dataclass(frozen=True)
class Heartbeat:
    t: float
    seq: int


@dataclass(frozen=True)
class Stuck:
    t: float
    reason: str


Event = Union[GameStarted, GameFinished, Heartbeat, Stuck]

_EVENTS: dict[str, type] = {
    "game_started": GameStarted, "game_finished": GameFinished, "heartbeat": Heartbeat, "stuck": Stuck,
}
_NAMES = {cls: name for name, cls in _EVENTS.items()}


def _decode(line: str) -> Event | None:
    try:
        data = json.loads(line)
        cls = _EVENTS[data.pop("event")]
        return cls(**data)
    except (ValueError, KeyError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Bot side
# ---------------------------------------------------------------------------

def has_repeating_cycle(moves: list) -> bool:
    """Whether the last moves repeat a pattern of period 1 to CYCLE_MAX_PERIOD
    at least CYCLE_MIN_REPS times in full."""
    n = len(moves)
    for period in range(1, CYCLE_MAX_PERIOD + 1):
        required = period * CYCLE_MIN_REPS
        if n < required:
            continue
        tail = moves[-required:]
        if all(tail[i] == tail[i % period] for i in range(required)):
            return True
    return False


class Reporter:
    """Writes the report as the bot plays. Every event is flushed when written."""

    def __init__(self, path: Path, clock: Clock = time.time) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = path.open("a", encoding="utf-8")
        self._clock = clock
        self._beat_at = float("-inf")
        self._beat_seq: int | None = None
        self._moves: deque = deque(maxlen=CYCLE_WINDOW)
        self._cycle_since: float | None = None
        self._stuck_told = False

    def _write(self, event: Event) -> None:
        self._fh.write(json.dumps({"event": _NAMES[type(event)], **event.__dict__}) + "\n")
        self._fh.flush()

    def game_started(self, explore: float) -> None:
        self._moves.clear()
        self._cycle_since, self._stuck_told = None, False
        self._beat_seq = None
        self._write(GameStarted(self._clock(), explore))

    def game_finished(self, won: bool, seed: str, ante: int) -> None:
        self._write(GameFinished(self._clock(), won, str(seed), int(ante)))

    def action(self, seq: int, state: str, method: str, params: dict | None) -> None:
        """One move about to be sent; *seq* counts the moves the game has taken this game."""
        now = self._clock()
        # A seq only goes out once it differs from the last one sent, so a bot whose
        # moves are all refused stops beating and reads as stalled.
        if seq != self._beat_seq and now - self._beat_at >= HEARTBEAT_EVERY:
            self._write(Heartbeat(now, seq))
            self._beat_at, self._beat_seq = now, seq

        self._moves.append((state, method, repr(params)))
        if len(self._moves) >= CYCLE_MIN_MOVES and has_repeating_cycle(list(self._moves)):
            if self._cycle_since is None:
                self._cycle_since = now
            elif not self._stuck_told and now - self._cycle_since >= CYCLE_HOLD:
                self._write(Stuck(now, f"repeating {state} {method} for {now - self._cycle_since:.0f}s"))
                self._stuck_told = True
        else:
            self._cycle_since, self._stuck_told = None, False

    def close(self) -> None:
        self._fh.close()


# ---------------------------------------------------------------------------
# Supervisor side
# ---------------------------------------------------------------------------

class Watcher:
    """Reads a report as it grows and keeps the totals the supervisor watches.

    Games and wins count every game finished in the file, across relaunches.
    ``last_move`` and ``stuck`` describe the bot launched last: ``bot_started``
    clears them.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._offset = 0
        self.games = 0
        self.wins = 0
        self.explore: float | None = None
        self.last_move: float | None = None
        self.stuck: str | None = None
        self._seq: int | None = None

    def read(self) -> list[Event]:
        """Events written since the last read. A line still being written waits for the next."""
        try:
            with self.path.open("rb") as fh:
                fh.seek(self._offset)
                raw = fh.read()
        except OSError:
            return []
        end = raw.rfind(b"\n") + 1
        self._offset += end
        events = []
        for line in raw[:end].decode("utf-8", errors="replace").splitlines():
            event = _decode(line)
            if event is not None:
                self._take(event)
                events.append(event)
        return events

    def _take(self, event: Event) -> None:
        if isinstance(event, GameStarted):
            self.explore = event.explore
            self._seq = None
            self.last_move = event.t
        elif isinstance(event, GameFinished):
            self.games += 1
            self.wins += event.won
            self.last_move = event.t
        elif isinstance(event, Heartbeat):
            if event.seq != self._seq:
                self._seq = event.seq
                self.last_move = event.t
        elif isinstance(event, Stuck):
            self.stuck = event.reason

    def bot_started(self) -> None:
        """A new bot takes over the report: forget the last one's moves.

        Read first: events still unread when this is called count from the new bot."""
        self.last_move = None
        self.stuck = None
        self._seq = None

    def stalled(self, now: float, timeout: float) -> bool:
        """No new move for *timeout* seconds, once the bot has made one."""
        return self.last_move is not None and now - self.last_move >= timeout
