"""Supervisor for Balatro bot instances.

Manages N (balatrobot serve + bot.py) pairs with health monitoring,
automatic restart, and clean shutdown.
"""

from __future__ import annotations

import argparse
import atexit
import importlib.util
import json
import logging
import os
import random
import shutil
import signal
import subprocess
import sys
import time
import zipfile
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from balatro_bot import dashboard_client, instance_report, jobs
from balatro_bot.config import LOG_DIR, REPO, WINS_DIR, SupervisorConfig, venv_python
from balatro_bot.game_server import GameServer, Jackdaw, LiveGame
from balatro_bot.instance_report import GameFinished, GameStarted, Watcher

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

cfg = SupervisorConfig()

BASE_PORT = cfg.base_port
BOT_MODULE = "balatro_bot.cli"


HEALTH_TIMEOUT = cfg.health_timeout
RESTART_COOLDOWN = cfg.restart_cooldown
RAPID_RESTART_WINDOW = cfg.rapid_restart_window
RAPID_RESTART_LIMIT = cfg.rapid_restart_limit
SERVER_STARTUP_WAIT = cfg.server_startup_wait
SERVER_STAGGER = cfg.server_stagger
BOT_STAGGER = cfg.bot_stagger
POLL_INTERVAL = cfg.poll_interval
# The batch is a job (balatro_bot.jobs): the panel reads its status, asks it to stop
# or pause and queues add/remove commands; a terminal supervisor honours them too. A
# paused batch lives on in its status (phase "paused") until --resume or --finish-paused.
MAX_INSTANCES = 100
# Removed slots keep their port so their logs and counts stay theirs; a batch
# hands out at most this many ports from its base port.
PORT_SPAN = 2 * MAX_INSTANCES
DEEP_RECOVERY_THRESHOLD = 5

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

log = logging.getLogger("supervisor")


BALATROBOT_LOGS_DIR = REPO / "logs"
BALATROBOT_LOGS_KEEP = 10

# Per-instance Balatro save directories. Each instance gets its own APPDATA
# override so Love2D writes meta.jkr/profile.jkr/save.jkr/settings.jkr into
# a dedicated folder — concurrent writes to the same .jkr files across
# instances corrupt meta.jkr (tutorial flag, unlocks).


def _code_version() -> str | None:
    """Short commit hash of the bot checkout, suffixed "-dirty" when tracked files differ."""
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=REPO,
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"], cwd=REPO,
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return f"{sha}-dirty" if dirty else sha


def prune_balatrobot_logs(keep: int = BALATROBOT_LOGS_KEEP) -> None:
    """Delete oldest session dirs in ./logs when count exceeds `keep`.

    Upstream `balatrobot serve` pipes Balatro+mod stdout/stderr into
    logs/<session>/<port>.log and never rotates — it can hit tens of GB
    over long batch runs. We only touch sessions not currently being
    written to (oldest first), so in-flight runs are safe.
    """
    if not BALATROBOT_LOGS_DIR.exists():
        return
    sessions = sorted(
        (p for p in BALATROBOT_LOGS_DIR.iterdir() if p.is_dir()),
        key=lambda p: p.stat().st_mtime,
    )
    excess = len(sessions) - keep
    if excess <= 0:
        return
    for old in sessions[:excess]:
        try:
            shutil.rmtree(old)
            log.info("Pruned old balatrobot log dir: %s", old.name)
        except OSError as e:
            log.warning("Could not prune %s: %s", old, e)


def setup_supervisor_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.DEBUG)
    fh = logging.FileHandler(LOG_DIR / "supervisor.log", mode="a", encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-5s %(message)s", datefmt="%H:%M:%S"))
    log.addHandler(fh)


# ---------------------------------------------------------------------------
# Disney princess names
# ---------------------------------------------------------------------------

PRINCESS_NAMES = [
    "Rapunzel", "Cinderella", "Aurora", "Ariel", "Belle", "Jasmine",
    "Pocahontas", "Mulan", "Tiana", "Merida", "Moana", "Elsa", "Anna",
    "Snow White", "Raya", "Mirabel", "Asha", "Vanellope", "Giselle",
    "Megara", "Esmeralda", "Nala", "Kida", "Eilonwy", "Tinker Bell",
]


def _pick_princess_name(taken: set[str]) -> str:
    """Pick a random princess name not already in use."""
    available = [n for n in PRINCESS_NAMES if n not in taken]
    if not available:
        available = list(PRINCESS_NAMES)  # all taken, allow duplicates
    return random.choice(available)


RECENT_WINDOW = 300  # finished games the panel's summary covers
RECENT_WINS = 12
RATE_WINDOW = 60.0   # seconds an instance's games-per-minute is measured over


# ---------------------------------------------------------------------------
# Slot
# ---------------------------------------------------------------------------

@dataclass
class Slot:
    port: int
    name: str = ""
    deck: str = "RED"
    stake: str = "WHITE"
    server_proc: subprocess.Popen | None = None
    bot_proc: subprocess.Popen | None = None
    state: str = "stopped"
    restarts: int = 0
    restart_times: list[float] = field(default_factory=list)
    consecutive_launch_failures: int = 0
    last_health: float = 0.0
    last_reason: str = ""
    log_file: Path | None = None
    # The instance report of this slot's batch; set once the batch number is known.
    report: Watcher | None = None
    bot_launch_time: float = 0.0
    # Rate the slot is set to; the bot picks it up from explore_file at its next game.
    explore_rate: float = 0.0
    # Rate the game in progress started with.
    explore_active: float = 0.0
    # [time, ante, won] per finished game, and the wins among them in full.
    recent: deque = field(default_factory=lambda: deque(maxlen=RECENT_WINDOW))
    recent_wins: deque = field(default_factory=lambda: deque(maxlen=RECENT_WINS))
    # (time, games completed) readings from the last RATE_WINDOW seconds.
    rate_samples: deque = field(default_factory=deque)

    @property
    def explore_file(self) -> Path:
        return LOG_DIR / str(self.port) / "explore_rate.txt"

    def is_alive(self, proc: subprocess.Popen | None) -> bool:
        return proc is not None and proc.poll() is None

    def kill_pair(self, server: GameServer) -> None:
        """Kill both server and bot process trees, including child consoles.

        Bot is force-killed (it doesn't write save files). The server stops its own
        way: the live game closes gracefully so Balatro finishes any save in flight.
        """
        # Bot first, force — it holds no on-disk state worth preserving.
        if self.bot_proc and self.bot_proc.poll() is None:
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(self.bot_proc.pid)],
                    capture_output=True,
                )
            except OSError:
                pass
        if self.server_proc and self.server_proc.poll() is None:
            server.stop(self.server_proc)
        self.server_proc = None
        self.bot_proc = None

    def check_health(self, server: GameServer) -> bool:
        if server.health(self.port):
            self.last_health = time.time()
            return True
        return False

    def read_explore_rate(self) -> None:
        """Pick up a rate the control panel wrote for this slot."""
        try:
            self.explore_rate = round(float(self.explore_file.read_text().strip()), 2)
        except (OSError, ValueError):
            pass

    def write_explore_rate(self) -> None:
        try:
            self.explore_file.write_text(f"{self.explore_rate}")
        except OSError as e:
            log.warning("%s (port %d): could not write explore rate: %s", self.name, self.port, e)

    @property
    def games_done(self) -> int:
        return self.report.games if self.report else 0

    @property
    def wins(self) -> int:
        return self.report.wins if self.report else 0

    def follow_report(self) -> None:
        """Read what the bot reported since last time: the explore rate in use and each finished game."""
        if self.report is None:
            return
        for event in self.report.read():
            if isinstance(event, GameStarted):
                self.explore_active = event.explore
            elif isinstance(event, GameFinished):
                self.recent.append([event.t, event.ante, event.won])
                if event.won:
                    self.recent_wins.append({
                        "name": self.name, "port": self.port, "seed": event.seed, "ante": event.ante,
                        "time": event.t,
                    })

    def soft_lock(self, now: float, stall_timeout: float) -> str | None:
        """Why the bot counts as soft-locked: it reported itself stuck, or made no move in time."""
        if self.report is None:
            return None
        if self.report.stuck:
            return self.report.stuck
        if self.report.stalled(now, stall_timeout):
            return f"no move for {stall_timeout:.0f}s"
        return None

    def games_per_minute(self, now: float, completed: int) -> float | None:
        """Games finished per minute over the last RATE_WINDOW seconds; None until
        ten seconds of readings are in."""
        samples = self.rate_samples
        samples.append((now, completed))
        while len(samples) > 2 and samples[1][0] <= now - RATE_WINDOW:
            samples.popleft()
        since, then = samples[0]
        if now - since < 10:
            return None
        return round(60 * (completed - then) / (now - since), 1)


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------

class Supervisor:
    def __init__(self, n: int, games: int, decks: list[str], stake: str,
                 seed: str | None, verbose: bool = False,
                 stream: bool = False, explore_instances: int = 0,
                 explore_rate: float = 0.0, base_port: int = BASE_PORT,
                 server: GameServer | None = None):
        taken: set[str] = set()
        slots = []
        for i in range(n):
            name = _pick_princess_name(taken)
            taken.add(name)
            # The first explore_instances slots explore; the rest play greedy as the baseline.
            rate = explore_rate if i < explore_instances else 0.0
            slots.append(Slot(port=base_port + i, name=name, deck=decks[i % len(decks)], stake=stake,
                              explore_rate=rate))
        self.slots = slots
        self.games = games
        self.decks = decks  # rotated across slots
        self.stake = stake
        self.base_port = base_port
        self.seed = seed
        self.verbose = verbose
        self.stream = stream
        self.explore_rate = explore_rate
        self.server = server or LiveGame(visible=stream)
        self.stall_timeout = self.server.stall_timeout
        self.bot_python = self.server.bot_python()
        self.running = True
        self.paused = False
        self.finished = False
        self.resumed = False
        self.session_num = 0
        self.session_start: float = 0.0
        self._shutdown_done = False
        self._finish_posted = False
        self.dashboard_batch_id: int | None = None

    def pause_state(self) -> dict:
        """Everything --resume needs to carry this batch on where it stopped."""
        return {
            "games": self.games, "decks": self.decks, "stake": self.stake, "seed": self.seed,
            "verbose": self.verbose, "explore_rate": self.explore_rate, "base_port": self.base_port,
            "sim": isinstance(self.server, Jackdaw), "session_num": self.session_num,
            "dashboard_batch_id": self.dashboard_batch_id,
            "elapsed": time.time() - self.session_start if self.session_start else 0.0,
            # Counts and recent games are read back from each slot's instance report.
            "report": True,
            "slots": [{
                "port": s.port, "name": s.name, "deck": s.deck, "stake": s.stake,
                "explore_rate": s.explore_rate, "restarts": s.restarts, "last_reason": s.last_reason,
                "state": s.state if s.state in ("done", "removed") else "stopped",
            } for s in self.slots],
        }

    @classmethod
    def from_pause_state(cls, state: dict, server: GameServer | None) -> Supervisor:
        sup = cls(n=0, games=state["games"], decks=state["decks"], stake=state["stake"],
                  seed=state["seed"], verbose=state["verbose"], explore_rate=state["explore_rate"],
                  base_port=state["base_port"], server=server)
        for s in state["slots"]:
            slot = Slot(port=s["port"], name=s["name"], deck=s["deck"], stake=s["stake"],
                        explore_rate=s["explore_rate"], restarts=s["restarts"],
                        last_reason=s["last_reason"], state=s["state"])
            sup.slots.append(slot)
        sup.session_num = state["session_num"]
        sup.dashboard_batch_id = state["dashboard_batch_id"]
        sup.session_start = time.time() - state["elapsed"]
        sup.resumed = True
        return sup

    def live_slots(self) -> list[Slot]:
        """Slots still playing or due to, i.e. not finished and not removed."""
        return [s for s in self.slots if s.state not in ("done", "removed")]

    def setup(self) -> None:
        WINS_DIR.mkdir(parents=True, exist_ok=True)
        for slot in self.slots:
            (LOG_DIR / str(slot.port)).mkdir(parents=True, exist_ok=True)

        ports = [s.port for s in self.slots]
        killed = jobs.kill_port_processes(ports)
        if killed:
            print(f"Cleaned up {killed} orphan processes")
            time.sleep(2)

        if self.resumed:
            # Each slot's instance report carries its count across the pause.
            if self.dashboard_batch_id is not None:
                atexit.register(self._atexit_abandon)
            for slot in self.slots:
                self._watch(slot)
                slot.write_explore_rate()
            return

        # Session number comes from the dashboard (MAX(batch_number)+1).
        # Fall back to scanning local log files only when the dashboard is
        # unreachable / unconfigured, so an offline run still gets a
        # plausible number for log filenames.
        result = dashboard_client.post_batch_start(
            num_instances=len(self.slots),
            games_per_inst=self.games,
            decks=",".join(self.decks),
            stake=self.stake,
            code_version=self._code_version(),
        )
        if result is not None:
            self.dashboard_batch_id, self.session_num = result
            # Last-resort safety net: if the process is torn down without
            # reaching shutdown() (e.g. uncaught crash), atexit still fires
            # and marks the batch as abandoned on the dashboard. shutdown()'s
            # own path is idempotent — post_run()'s post_batch_finish will
            # have already set the status and this becomes a no-op-ish
            # second call.
            atexit.register(self._atexit_abandon)

            # Snapshot the joker value map at batch start so the dashboard can
            # show what the bot "thinks" each joker is worth under canonical
            # scenarios. Per-batch so valuator tuning is traceable over time.
            # Run in a background thread — 135 jokers × 100+ scenarios takes
            # tens of seconds and we don't want to block batch startup.
            import threading

            def _snapshot_value_map(batch_id: int) -> None:
                try:
                    from balatro_bot.value_map import (
                        build_value_map, scenario_labels, archetype_names, ANTES,
                    )
                    payload = {
                        "scenarios": scenario_labels(),
                        "archetypes": archetype_names(),
                        "antes": ANTES,
                        "rows": build_value_map(),
                    }
                    dashboard_client.post_value_map(batch_id, payload)
                    log.info("value map snapshot pushed for batch %d", batch_id)
                except Exception:
                    log.warning("value map snapshot failed", exc_info=True)

            threading.Thread(
                target=_snapshot_value_map,
                args=(self.dashboard_batch_id,),
                daemon=True,
                name="value-map-snapshot",
            ).start()
        else:
            self.dashboard_batch_id = None
            self.session_num = instance_report.next_local_number(LOG_DIR)
            log.warning("Dashboard unavailable — using local session number %d", self.session_num)

        for slot in self.slots:
            self._watch(slot)
            slot.write_explore_rate()

        prune_balatrobot_logs()

    def _child_flags(self) -> int:
        """Stream mode keeps visible consoles to watch; batches give each child a hidden one.

        Children still get their own console (CREATE_NO_WINDOW), so their
        output has somewhere to go and they never share the supervisor's.
        Everything they log already lands in bot_log/ and logs/.

        Batch children run below normal priority so the desktop stays
        responsive; Balatro inherits it through the serve process.
        """
        if self.stream:
            return subprocess.CREATE_NEW_CONSOLE
        return subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS

    def _code_version(self) -> str | None:
        """The checkout's version, tagged with its game server's (+jackdaw on the simulator)."""
        version = _code_version()
        return version + self.server.version_tag if version else version

    def _watch(self, slot: Slot) -> None:
        """Point the slot at its files for this batch and read what its report already holds."""
        files = instance_report.batch_files(slot.port, self.session_num, LOG_DIR, stream=self.stream)
        slot.log_file = files.game_log
        slot.report = Watcher(files.report)
        slot.follow_report()

    def launch_server(self, slot: Slot) -> None:
        slot.server_proc = self.server.start(slot.port, self._child_flags())
        slot.state = "starting"
        slot.last_health = time.time()

    def _remaining_games(self, slot: Slot) -> int:
        slot.follow_report()
        return max(self.games - slot.games_done, 0)

    def launch_bot(self, slot: Slot) -> None:
        games = self._remaining_games(slot)
        completed = self.games - games
        deck = slot.deck
        cmd = [
            self.bot_python, "-m", BOT_MODULE,
            "--start", "--port", str(slot.port),
            "--games", str(games),
            "--games-offset", str(completed),
            "--deck", deck,
            "--stake", slot.stake,
            "--batch-number", str(self.session_num),
        ]
        if self.seed:
            cmd.extend(["--seed", self.seed])
        if self.verbose:
            cmd.append("--verbose")
        if self.stream:
            cmd.extend(["--stream-delay", "2.0", "--stream-log"])
        if self.dashboard_batch_id:
            cmd.extend(["--dashboard-batch-id", str(self.dashboard_batch_id)])
        slot.read_explore_rate()
        slot.write_explore_rate()
        cmd.extend(["--explore-file", str(slot.explore_file)])
        if slot.explore_rate > 0:
            cmd.extend(["--explore", str(slot.explore_rate)])
        slot.explore_active = slot.explore_rate
        slot.bot_proc = subprocess.Popen(
            cmd,
            creationflags=self._child_flags(),
        )
        slot.state = "running"
        if slot.report is not None:
            slot.report.bot_started()
        slot.bot_launch_time = time.time()
        log.info("%s (port %d): launching with %d games remaining [%s deck]", slot.name, slot.port, games, deck)

    def wait_for_health(self, slot: Slot, timeout: float = 30.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline and self.running:
            if slot.check_health(self.server):
                return True
            if not slot.is_alive(slot.server_proc):
                return False
            time.sleep(1.0)
        return False

    def start_slot(self, slot: Slot) -> None:
        self.launch_server(slot)
        log.info("%s (port %d): server starting (pid=%s)", slot.name, slot.port,
                 slot.server_proc.pid if slot.server_proc else "?")
        if self.wait_for_health(slot, timeout=SERVER_STARTUP_WAIT):
            self.launch_bot(slot)
            log.info("%s (port %d): bot started (pid=%s)", slot.name, slot.port,
                     slot.bot_proc.pid if slot.bot_proc else "?")
        else:
            log.warning("%s (port %d): server slow to start, launching bot anyway", slot.name, slot.port)
            self.launch_bot(slot)
            slot.last_health = time.time()
        time.sleep(BOT_STAGGER)

    def restart_slot(self, slot: Slot, reason: str = "unknown") -> None:
        slot.last_reason = reason
        now = time.time()
        slot.restart_times = [t for t in slot.restart_times
                              if now - t < RAPID_RESTART_WINDOW]
        if len(slot.restart_times) >= RAPID_RESTART_LIMIT:
            slot.state = "cooldown"
            log.warning("%s (port %d): cooldown — %d restarts in %ds (last: %s)",
                        slot.name, slot.port, len(slot.restart_times), RAPID_RESTART_WINDOW, reason)
            return

        slot.restart_times.append(now)
        slot.restarts += 1
        slot.state = "restarting"

        # Rotate to a new princess name
        taken = {s.name for s in self.slots if s is not slot}
        old_name = slot.name
        taken.add(old_name)  # don't reuse the same name
        slot.name = _pick_princess_name(taken)
        log.warning("%s (port %d): restart #%d as %s — %s",
                    old_name, slot.port, slot.restarts, slot.name, reason)

        slot.kill_pair(self.server)

        # Deep recovery: if the bot has been failing to launch repeatedly, a
        # bare kill_pair isn't enough — there's likely a zombie Balatro.exe
        # still holding resources. Nuke anything on the port and wait longer.
        if slot.consecutive_launch_failures >= DEEP_RECOVERY_THRESHOLD:
            log.warning("%s (port %d): deep recovery — %d consecutive launch failures, killing port processes",
                        slot.name, slot.port, slot.consecutive_launch_failures)
            jobs.kill_port_processes([slot.port])
            slot.consecutive_launch_failures = 0
            time.sleep(10)
        else:
            time.sleep(2)
        self.start_slot(slot)

    def add_slot(self, deck: str, stake: str) -> Slot | None:
        """Start one more instance on the next unused port; None when the batch is full."""
        port = self.base_port + len(self.slots)
        if len(self.live_slots()) >= MAX_INSTANCES or len(self.slots) >= PORT_SPAN:
            log.warning("Add instance ignored: batch is full")
            return None
        slot = Slot(port=port, name=_pick_princess_name({s.name for s in self.slots}), deck=deck, stake=stake)
        (LOG_DIR / str(port)).mkdir(parents=True, exist_ok=True)
        jobs.kill_port_processes([port])
        self._watch(slot)
        slot.write_explore_rate()
        self.slots.append(slot)
        log.info("%s (port %d): added [%s deck, %s stake]", slot.name, port, deck, stake)
        self.start_slot(slot)
        return slot

    def remove_slot(self, port: int) -> None:
        """Stop one instance for the rest of the batch; the games it finished stay counted."""
        slot = next((s for s in self.live_slots() if s.port == port), None)
        if slot is None:
            return
        slot.kill_pair(self.server)
        slot.state = "removed"
        log.info("%s (port %d): removed", slot.name, slot.port)

    def apply_commands(self) -> None:
        """Carry out the add/remove requests the control panel queued, oldest first."""
        for cmd in jobs.batch().take():
            if cmd.get("op") == "add":
                self.add_slot(cmd["deck"], cmd["stake"])
            elif cmd.get("op") == "remove":
                self.remove_slot(int(cmd["port"]))

    def check_slot(self, slot: Slot) -> None:
        if slot.state in ("done", "stopped", "removed"):
            return

        if slot.state == "cooldown":
            latest = max(slot.restart_times) if slot.restart_times else 0
            if time.time() - latest > RESTART_COOLDOWN:
                self.restart_slot(slot)
            return

        slot.follow_report()
        why = slot.soft_lock(time.time(), self.stall_timeout) if not self.stream and slot.state == "running" else None
        if why:
            log.warning("%s (port %d): soft-lock detected (%s) — restarting", slot.name, slot.port, why)
            self.restart_slot(slot, reason="soft-lock detected")
            return

        if slot.bot_proc and slot.bot_proc.poll() is not None:
            rc = slot.bot_proc.returncode
            if rc == 0:
                slot.state = "done"
                log.info("%s (port %d): finished (%d/%d)", slot.name, slot.port, slot.games_done, self.games)
                slot.kill_pair(self.server)
                return
            else:
                alive_for = time.time() - slot.bot_launch_time if slot.bot_launch_time else 0.0
                # A bot that ran >120s before crashing is considered "healthy launch";
                # reset the consecutive-failure counter. Fast failures accumulate.
                if alive_for > 120.0:
                    slot.consecutive_launch_failures = 0
                else:
                    slot.consecutive_launch_failures += 1
                log.error("%s (port %d): bot exited rc=%d (alive=%.0fs, consec_fails=%d)",
                          slot.name, slot.port, rc, alive_for, slot.consecutive_launch_failures)
                self.restart_slot(slot, reason=f"bot exited rc={rc}")
                return

        if not slot.is_alive(slot.server_proc):
            server_rc = slot.server_proc.returncode if slot.server_proc else "?"
            log.error("%s (port %d): server died rc=%s", slot.name, slot.port, server_rc)
            self.restart_slot(slot, reason=f"server died rc={server_rc}")
            return

    def _status(self, phase: str) -> dict:
        """Snapshot of every slot and the batch totals, for the console, the GUI and the dashboard."""
        slots = []
        now = time.time()
        for slot in self.slots:
            slot.follow_report()
            slot.read_explore_rate()
            last_action = (slot.report and slot.report.last_move) or slot.bot_launch_time
            completed = slot.games_done
            if slot.state == "running":
                games_per_min = slot.games_per_minute(now, completed)
            else:
                games_per_min = None
                slot.rate_samples.clear()
            slots.append({
                "port": slot.port,
                "name": slot.name,
                "deck": slot.deck,
                "stake": slot.stake,
                "explore_rate": slot.explore_rate,
                "explore_active": slot.explore_active,
                "games_per_min": games_per_min,
                "idle_secs": int(now - last_action) if slot.state == "running" and last_action else None,
                "log": str(slot.log_file) if slot.log_file else "",
                "state": slot.state,
                "progress": f"{completed}/{self.games}" if slot.bot_launch_time or completed else "",
                "games_completed": completed,
                "wins": slot.wins,
                "restarts": slot.restarts,
                "last_reason": slot.last_reason,
            })
        completed = sum(s["games_completed"] for s in slots)
        # A removed slot's batch share ends at the games it finished.
        total_expected = sum(s["games_completed"] if s["state"] == "removed" else self.games for s in slots)
        elapsed = time.time() - self.session_start if self.session_start else 0
        rate = completed / (elapsed / 3600) if elapsed > 30 and completed > 0 else 0.0
        eta_secs = int((total_expected - completed) / (rate / 3600)) if rate > 0 else None
        recent = sorted((g for s in self.slots for g in s.recent), reverse=True)[:RECENT_WINDOW]
        lost_at: dict[int, int] = {}
        for _, ante, won in recent:
            if not won:
                lost_at[ante] = lost_at.get(ante, 0) + 1
        return {
            "phase": phase,
            "started": self.session_start,
            "session": self.session_num,
            "games_per_instance": self.games,
            "slots": slots,
            "completed": completed,
            "total_expected": total_expected,
            "wins": sum(s["wins"] for s in slots),
            "rate_per_hour": round(rate, 1),
            "eta_secs": eta_secs,
            "explore_rate": self.explore_rate,
            "stall_secs": self.stall_timeout,
            # The last RECENT_WINDOW finished games: how many, how many won, and the
            # ante each of the rest ended at.
            "recent": {"games": len(recent), "wins": sum(1 for g in recent if g[2]), "lost_at": lost_at},
            "recent_wins": sorted((g for s in self.slots for g in s.recent_wins),
                                  key=lambda g: g["time"], reverse=True)[:RECENT_WINS],
        }

    def _write_status(self, status: dict) -> None:
        jobs.batch().report(status)

    def print_status(self) -> None:
        status = self._status("running")
        self._write_status(status)

        os.system("cls" if os.name == "nt" else "clear")
        print("  Mother Gothel")
        print("  " + "=" * 50)
        print()
        state_icons = {
            "stopped": ".", "starting": "~", "running": "+", "restarting": "!",
            "cooldown": "Z", "done": "*", "dead": "X",
        }
        for s in status["slots"]:
            restarts = f" (restarts: {s['restarts']})" if s["restarts"] else ""
            deck = f"{s['deck']}*" if s["explore_rate"] > 0 else s["deck"]
            state_str = f"{s['state']} ({s['last_reason']})" if s["last_reason"] else s["state"]
            print(f"  [{state_icons.get(s['state'], '?')}] {s['name']:<12} {deck:<8} "
                  f"{s['progress'] or '...':>12}  {'W:' + str(s['wins']):<6} {state_str}{restarts}")
        print()
        active = sum(1 for s in status["slots"] if s["state"] in ("running", "starting"))
        done = sum(1 for s in status["slots"] if s["state"] == "done")
        completed = status["completed"]
        win_pct = f" ({100 * status['wins'] / completed:.1f}%)" if completed > 0 else ""
        rate_str = ""
        if status["eta_secs"] is not None:
            h, m = divmod(status["eta_secs"] // 60, 60)
            rate_str = f"  Rate: {status['rate_per_hour']:.0f} g/hr  ETA: ~{h}h{m:02d}m"
        print(f"  Active: {active}  Done: {done}  Wins: {status['wins']}{win_pct}  Session: {self.session_num}{rate_str}")
        print(f"  Ctrl+C to stop all")

        if self.dashboard_batch_id:
            dashboard_client.post_instance_states(self.dashboard_batch_id, [
                {
                    "port": s["port"],
                    "name": s["name"],
                    "deck": s["deck"],
                    "state": s["state"],
                    "games_completed": s["games_completed"],
                    "games_total": self.games,
                    "wins": s["wins"],
                    "restarts": s["restarts"],
                    "last_reason": s["last_reason"],
                    "explore_rate": s["explore_rate"],
                }
                for s in status["slots"]
            ])

    def run(self) -> None:
        setup_supervisor_logging()
        self.setup()
        if not self.resumed:
            self.session_start = time.time()
        to_start = self.live_slots()
        log.info("=== Mother Gothel %s: %d princesses, %d games each ===",
                 "resumed" if self.resumed else "started", len(to_start), self.games)
        names = ", ".join(s.name for s in to_start)
        print(f"  Mother Gothel is sending {len(to_start)} princesses to play Balatro")
        print(f"  ({names})\n")
        print(f"  Session {self.session_num}, {self.games} games each\n")
        if isinstance(self.server, Jackdaw):
            log.info("Simulator batch")
            print("  Simulator batch\n")

        self._write_status(self._status("starting"))
        for i, slot in enumerate(to_start):
            if not self.running or jobs.batch().asked("stop") or jobs.batch().asked("pause"):
                break
            self.start_slot(slot)
            self._write_status(self._status("starting"))
            if i < len(to_start) - 1:
                time.sleep(SERVER_STAGGER)

        last_status = 0.0
        while self.running:
            if jobs.batch().asked("stop"):
                log.info("Stop asked, shutting down")
                break
            if jobs.batch().asked("pause"):
                log.info("Pause asked, pausing")
                self.paused = True
                break

            self.apply_commands()
            for slot in self.slots:
                self.check_slot(slot)

            if time.time() - last_status > POLL_INTERVAL:
                self.print_status()
                last_status = time.time()

            active_states = {"starting", "running", "restarting", "cooldown"}
            if not any(s.state in active_states for s in self.slots):
                self.print_status()
                done = sum(1 for s in self.slots if s.state == "done")
                print(f"\n  All princesses have returned ({done} finished).")
                self.finished = True
                break

            time.sleep(1.0)

    def shutdown(self) -> None:
        if self._shutdown_done:
            return
        self._shutdown_done = True
        self.running = False
        log.info("Pause requested" if self.paused else "Shutdown requested")
        print("\n  Pausing..." if self.paused else "\n  Shutting down...")
        self._write_status(self._status("pausing" if self.paused else "stopping"))
        for slot in self.slots:
            slot.kill_pair(self.server)
        # Sweep ports for any orphans that escaped process tree kill
        ports = [s.port for s in self.slots]
        jobs.kill_port_processes(ports)
        total_restarts = sum(s.restarts for s in self.slots)
        log.info("Shutdown complete. Total restarts: %d", total_restarts)
        if self.paused:
            # Counts, logs and the dashboard batch stay open for --resume.
            for slot in self.live_slots():
                slot.state = "stopped"
            self._write_status({**self._status("paused"), "resume": self.pause_state()})
            jobs.batch().clear()
            print("  Batch paused.")
            return
        if self.session_num > 0:
            self.post_run()
        self._write_status(self._status("finished" if self.finished else "stopped"))
        jobs.batch().clear()
        print("  All princesses recalled.")

    def _atexit_abandon(self) -> None:
        """Last-resort fallback if the process dies before post_run runs.

        No-op once a clean finish has been posted, or while the batch is paused.
        """
        if self._finish_posted or self.paused or self.dashboard_batch_id is None:
            return
        try:
            dashboard_client.post_batch_finish(self.dashboard_batch_id, status="abandoned")
        except Exception:
            pass

    def post_run(self) -> None:
        # Mark the batch finished FIRST — stats/rotate below can crash (shared
        # FS, stats script bugs, etc.) and we don't want that to orphan the
        # batch at "running" on the dashboard.
        if self.dashboard_batch_id:
            dashboard_client.post_batch_finish(self.dashboard_batch_id)
            self._finish_posted = True

        batch = f"{self.session_num:03d}"
        print(f"\n  Running stats for batch {batch}...")
        try:
            result = subprocess.run(
                [venv_python("bot"), "-m", "stats", batch],
                cwd=str(REPO),
                capture_output=False,
            )
            if result.returncode != 0:
                log.warning("stats exited with rc=%d", result.returncode)
        except Exception as e:
            log.warning("Failed to run stats: %s", e)
        self._rotate_logs()

    def _rotate_logs(self, keep: int = 10) -> None:
        archive_dir = LOG_DIR / "archive"
        archive_dir.mkdir(exist_ok=True)

        to_archive = sorted(instance_report.batch_numbers(LOG_DIR), reverse=True)[keep:]
        if not to_archive:
            return

        stats_out = LOG_DIR / "stats_output"
        moved = 0
        for n in to_archive:
            dest = archive_dir / f"batch_{n:03d}"
            dest.mkdir(exist_ok=True)
            for port_dir in LOG_DIR.iterdir():
                if not port_dir.name.isdigit():
                    continue
                port = port_dir.name
                files = instance_report.batch_files(int(port), n, LOG_DIR)
                for src in (files.game_log, files.scoring_log, files.report):
                    if src.exists():
                        stem, rest = src.name.split("_", 1)
                        shutil.move(str(src), str(dest / f"{stem}_{port}_{rest}"))
                        moved += 1
                if files.wins_log.exists():
                    shutil.move(str(files.wins_log), str(dest / files.wins_log.name))
                    moved += 1
            for md in (f"batch_{n:03d}.md", f"batch_{n:03d}_wins.md"):
                src = stats_out / md
                if src.exists():
                    shutil.move(str(src), str(dest / md))
                    moved += 1

        log.info("Archived %d files for %d batches", moved, len(to_archive))

        # Zip if archive has 50+ subdirs
        loose = sorted(
            [d for d in archive_dir.iterdir() if d.is_dir() and d.name.startswith("batch_")],
            key=lambda d: d.name,
        )
        if len(loose) < 50:
            return

        first = loose[0].name.replace("batch_", "")
        last = loose[-1].name.replace("batch_", "")
        zip_path = archive_dir / f"archive_batch_{first}_to_{last}.zip"
        file_count = 0
        print(f"  Zipping {len(loose)} archive batches → {zip_path.name}...")
        try:
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for batch_dir in loose:
                    for f in batch_dir.rglob("*"):
                        if f.is_file():
                            zf.write(f, f.relative_to(archive_dir))
                            file_count += 1
            for batch_dir in loose:
                shutil.rmtree(batch_dir)
            log.info("Zipped %d files into %s", file_count, zip_path.name)
            print(f"  Zipped {file_count} files.")
        except Exception as e:
            log.error("Failed to create archive zip: %s", e)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Mother Gothel — Balatro bot supervisor")
    parser.add_argument("-n", "--instances", type=int, default=6,
                        help="Number of bot instances (default: 6)")
    parser.add_argument("--games", type=int, default=1000,
                        help="Games per instance (default: 1000)")
    parser.add_argument("--deck", default="RED",
                        help="Deck type(s). Comma-separated to rotate across instances (e.g. RED,BLUE)")
    parser.add_argument("--stake", default="WHITE", help="Stake level")
    parser.add_argument("--seed", default=None, help="Game seed")
    parser.add_argument("--kill", action="store_true",
                        help="Kill all existing bot/balatro processes and exit")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Pass --verbose to bot instances (DEBUG logging)")
    parser.add_argument("--stream", action="store_true",
                        help="Stream mode: single headed instance, no fast-forward, 2s action delay")
    parser.add_argument("--explore-instances", type=int, default=0,
                        help="How many instances explore; the rest are the greedy baseline (default: 0)")
    parser.add_argument("--explore-rate", type=float, default=0.15,
                        help="Fraction of shop and pack decisions an exploring instance explores (default: 0.15)")
    parser.add_argument("--base-port", type=int, default=BASE_PORT,
                        help=f"First instance's port (default: {BASE_PORT})")
    parser.add_argument("--sim", action="store_true",
                        help="Play on the Jackdaw simulator instead of Balatro")
    parser.add_argument("--resume", action="store_true",
                        help="Carry on the paused batch (other batch flags are ignored)")
    parser.add_argument("--finish-paused", action="store_true",
                        help="End the paused batch: post its finish, run stats, rotate logs")
    args = parser.parse_args()

    pause_state = None
    if args.resume or args.finish_paused:
        saved = jobs.batch().status() or {}
        if saved.get("phase") != "paused" or "resume" not in saved:
            parser.error("no paused batch to resume or finish")
        pause_state = saved["resume"]
        args.sim = pause_state["sim"]
        if args.resume and not pause_state.get("report"):
            parser.error("this batch was paused before instance reports: end it with --finish-paused")

    if importlib.util.find_spec("jackpotts_kernel") is None:
        parser.error("the compiled scoring kernel is not built into this environment "
                     "(see docs/installation.md)")

    server: GameServer = LiveGame(visible=args.stream)
    if args.sim:
        server = Jackdaw()
        if not server.available():
            parser.error(f"--sim: no jackdaw interpreter at {server.python} "
                         "(create .venv-jackdaw with jackdaw installed, or set $JACKDAW_PYTHON)")

    if args.stream:
        args.instances = 1
        os.system("mode con: cols=74 lines=13")

    if args.kill:
        ports = list(range(args.base_port, args.base_port + args.instances))
        killed = jobs.kill_port_processes(ports)
        print(f"Killed {killed} processes")
        return

    if args.finish_paused:
        sup = Supervisor.from_pause_state(pause_state, server)
        jobs.batch().claim()
        setup_supervisor_logging()
        sup.shutdown()
        return

    decks = [d.strip() for d in args.deck.split(",")]
    if args.explore_instances > 0 and len(decks) > 1:
        # Explore and baseline instances must differ only in exploration;
        # rotate decks between batches instead.
        parser.error("--explore-instances needs a single --deck")
    if pause_state is not None:
        sup = Supervisor.from_pause_state(pause_state, server)
    else:
        sup = Supervisor(
            n=args.instances,
            games=args.games,
            decks=decks,
            stake=args.stake,
            seed=args.seed,
            verbose=args.verbose,
            stream=args.stream,
            explore_instances=args.explore_instances,
            explore_rate=args.explore_rate,
            base_port=args.base_port,
            server=server,
        )

    jobs.batch().claim()

    def on_sigint(sig, frame):
        sup.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, on_sigint)
    signal.signal(signal.SIGTERM, on_sigint)
    jobs.tie_children()

    try:
        sup.run()
    finally:
        sup.shutdown()


if __name__ == "__main__":
    main()
