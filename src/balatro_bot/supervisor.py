"""Supervisor for Balatro bot instances.

Manages N (balatrobot serve + bot.py) pairs with health monitoring,
automatic restart, and clean shutdown.
"""

from __future__ import annotations

import argparse
import logging
import os
import random
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import zipfile
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from balatro_bot.config import SupervisorConfig

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

cfg = SupervisorConfig()

BASE_PORT = cfg.base_port
BASE_DIR = Path(__file__).parent.parent.parent
BOT_MODULE = "balatro_bot.cli"
LOG_DIR = cfg.log_dir
WINS_DIR = cfg.wins_dir

LOVE_PATH = cfg.love_path
LOVELY_PATH = cfg.lovely_path
UVX = cfg.uvx_path
PYTHON = cfg.python_path or str(BASE_DIR / ".venv" / "Scripts" / "python.exe")

HEALTH_TIMEOUT = cfg.health_timeout
RESTART_COOLDOWN = cfg.restart_cooldown
RAPID_RESTART_WINDOW = cfg.rapid_restart_window
RAPID_RESTART_LIMIT = cfg.rapid_restart_limit
SERVER_STARTUP_WAIT = cfg.server_startup_wait
SERVER_STAGGER = cfg.server_stagger
BOT_STAGGER = cfg.bot_stagger
POLL_INTERVAL = cfg.poll_interval

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

log = logging.getLogger("supervisor")


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


# ---------------------------------------------------------------------------
# Slot
# ---------------------------------------------------------------------------

@dataclass
class Slot:
    port: int
    name: str = ""
    server_proc: subprocess.Popen | None = None
    bot_proc: subprocess.Popen | None = None
    state: str = "stopped"
    restarts: int = 0
    restart_times: list[float] = field(default_factory=list)
    last_health: float = 0.0
    progress: str = ""
    wins: int = 0
    last_reason: str = ""
    log_file: Path | None = None
    log_offset: int = 0
    recent_lines: deque = field(default_factory=lambda: deque(maxlen=40))
    loop_start: float = 0.0

    def is_alive(self, proc: subprocess.Popen | None) -> bool:
        return proc is not None and proc.poll() is None

    def kill_pair(self) -> None:
        """Kill both server and bot process trees, including child consoles."""
        for proc in (self.bot_proc, self.server_proc):
            if proc and proc.poll() is None:
                # taskkill /F /T kills the entire process tree (including
                # Balatro.exe spawned by uvx in a separate console window)
                try:
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                        capture_output=True,
                    )
                except OSError:
                    pass
        # Give processes a moment to die, then force-kill any stragglers
        time.sleep(1.0)
        for proc in (self.bot_proc, self.server_proc):
            if proc and proc.poll() is None:
                try:
                    proc.kill()
                except OSError:
                    pass
        self.server_proc = None
        self.bot_proc = None

    def check_health(self) -> bool:
        try:
            with socket.create_connection(("localhost", self.port), timeout=2):
                self.last_health = time.time()
                return True
        except (OSError, TimeoutError):
            pass
        return False

    def read_progress(self) -> str:
        try:
            p = LOG_DIR / str(self.port) / "progress.txt"
            if p.exists():
                self.progress = p.read_text().strip()
        except OSError:
            pass
        return self.progress

    def read_wins(self, session_num: int) -> None:
        try:
            p = WINS_DIR / f"wins_{self.port}_{session_num:03d}.log"
            if p.exists():
                self.wins = sum(1 for line in p.read_text(encoding="utf-8", errors="replace").splitlines()
                                if line.startswith("VICTORY"))
        except OSError:
            pass

    _LOG_PREFIX = re.compile(r"^\d{2}:\d{2}:\d{2} \S+ ")

    def poll_log_for_softlock(self) -> bool:
        if not self.log_file:
            return False
        try:
            with self.log_file.open("rb") as fh:
                fh.seek(self.log_offset)
                raw = fh.read()
                self.log_offset = fh.tell()
        except OSError:
            return False

        for line in raw.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if line:
                self.recent_lines.append(self._LOG_PREFIX.sub("", line))

        if len(self.recent_lines) < 20:
            return False

        unique_ratio = len(set(self.recent_lines)) / len(self.recent_lines)
        if unique_ratio <= 0.25:
            if self.loop_start == 0.0:
                self.loop_start = time.time()
            return time.time() - self.loop_start >= 60
        else:
            self.loop_start = 0.0
            return False


# ---------------------------------------------------------------------------
# Process cleanup
# ---------------------------------------------------------------------------

def kill_port_processes(ports: list[int]) -> int:
    killed = 0
    try:
        result = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True
        )
        pids_to_kill: set[str] = set()
        for line in result.stdout.splitlines():
            for port in ports:
                if f":{port}" in line and ("LISTENING" in line or "ESTABLISHED" in line):
                    parts = line.split()
                    pid = parts[-1]
                    if pid.isdigit() and pid != "0":
                        pids_to_kill.add(pid)
        for pid in pids_to_kill:
            subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
            killed += 1
    except Exception:
        pass
    return killed


def compute_session_number() -> int:
    nums = []
    for f in LOG_DIR.glob("*/game_*.log"):
        m = re.match(r"game_(\d+)\.log", f.name)
        if m:
            nums.append(int(m.group(1)))
    return (max(nums) + 1) if nums else 1


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------

class Supervisor:
    def __init__(self, n: int, games: int, decks: list[str], stake: str,
                 seed: str | None, verbose: bool = False):
        taken: set[str] = set()
        slots = []
        for i in range(n):
            name = _pick_princess_name(taken)
            taken.add(name)
            slots.append(Slot(port=BASE_PORT + i, name=name))
        self.slots = slots
        self.games = games
        self.decks = decks  # rotated across slots
        self.stake = stake
        self.seed = seed
        self.verbose = verbose
        self.running = True
        self.session_num = 0
        self.session_start: float = 0.0
        self._shutdown_done = False

    def setup(self) -> None:
        WINS_DIR.mkdir(parents=True, exist_ok=True)
        for slot in self.slots:
            (LOG_DIR / str(slot.port)).mkdir(parents=True, exist_ok=True)

        ports = [s.port for s in self.slots]
        killed = kill_port_processes(ports)
        if killed:
            print(f"Cleaned up {killed} orphan processes")
            time.sleep(2)

        self.session_num = compute_session_number()
        (LOG_DIR / "next_num.txt").write_text(str(self.session_num))

        # Clear stale progress files from previous sessions
        for slot in self.slots:
            p = LOG_DIR / str(slot.port) / "progress.txt"
            if p.exists():
                p.unlink()

    def launch_server(self, slot: Slot) -> None:
        slot.server_proc = subprocess.Popen(
            [UVX, "balatrobot", "serve",
             "--port", str(slot.port),
             "--headless", "--fast",
             "--love-path", LOVE_PATH,
             "--lovely-path", LOVELY_PATH],
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        slot.state = "starting"
        slot.last_health = time.time()

    def _remaining_games(self, slot: Slot) -> int:
        """Parse progress.txt to find how many games are left."""
        try:
            p = LOG_DIR / str(slot.port) / "progress.txt"
            if p.exists():
                text = p.read_text().strip()
                if "/" in text:
                    done, _ = text.split("/", 1)
                    completed = int(done)
                    remaining = self.games - completed
                    if remaining > 0:
                        return remaining
        except (OSError, ValueError):
            pass
        return self.games

    def _deck_for_slot(self, slot: Slot) -> str:
        idx = self.slots.index(slot)
        return self.decks[idx % len(self.decks)]

    def launch_bot(self, slot: Slot) -> None:
        games = self._remaining_games(slot)
        completed = self.games - games
        deck = self._deck_for_slot(slot)
        cmd = [
            PYTHON, "-m", BOT_MODULE,
            "--start", "--port", str(slot.port),
            "--games", str(games),
            "--games-offset", str(completed),
            "--deck", deck,
            "--stake", self.stake,
        ]
        if self.seed:
            cmd.extend(["--seed", self.seed])
        if self.verbose:
            cmd.append("--verbose")
        slot.bot_proc = subprocess.Popen(
            cmd,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        slot.state = "running"
        slot.log_file = LOG_DIR / str(slot.port) / f"game_{self.session_num:03d}.log"
        slot.log_offset = 0
        slot.recent_lines.clear()
        slot.loop_start = 0.0
        log.info("%s (port %d): launching with %d games remaining [%s deck]", slot.name, slot.port, games, deck)

    def wait_for_health(self, slot: Slot, timeout: float = 30.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline and self.running:
            if slot.check_health():
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

        slot.kill_pair()
        time.sleep(2)
        self.start_slot(slot)

    def check_slot(self, slot: Slot) -> None:
        if slot.state == "done" or slot.state == "stopped":
            return

        if slot.state == "cooldown":
            latest = max(slot.restart_times) if slot.restart_times else 0
            if time.time() - latest > RESTART_COOLDOWN:
                self.restart_slot(slot)
            return

        if slot.state == "running" and slot.log_file and slot.poll_log_for_softlock():
            log.warning("%s (port %d): soft-lock detected — restarting", slot.name, slot.port)
            self.restart_slot(slot, reason="soft-lock detected")
            return

        if slot.bot_proc and slot.bot_proc.poll() is not None:
            rc = slot.bot_proc.returncode
            if rc == 0:
                slot.state = "done"
                slot.read_progress()
                log.info("%s (port %d): finished (%s)", slot.name, slot.port, slot.progress)
                slot.kill_pair()
                return
            else:
                log.error("%s (port %d): bot exited rc=%d", slot.name, slot.port, rc)
                self.restart_slot(slot, reason=f"bot exited rc={rc}")
                return

        if not slot.is_alive(slot.server_proc):
            server_rc = slot.server_proc.returncode if slot.server_proc else "?"
            log.error("%s (port %d): server died rc=%s", slot.name, slot.port, server_rc)
            self.restart_slot(slot, reason=f"server died rc={server_rc}")
            return

    def print_status(self) -> None:
        os.system("cls" if os.name == "nt" else "clear")
        print("  Mother Gothel")
        print("  " + "=" * 50)
        print()
        for slot in self.slots:
            slot.read_progress()
            slot.read_wins(self.session_num)
            restarts = f" (restarts: {slot.restarts})" if slot.restarts else ""
            prog = slot.progress or "..."
            state_icon = {
                "stopped": ".",
                "starting": "~",
                "running": "+",
                "restarting": "!",
                "cooldown": "Z",
                "done": "*",
                "dead": "X",
            }.get(slot.state, "?")
            deck = self._deck_for_slot(slot)
            name_pad = f"{slot.name:<12}"
            wins_col = f"W:{slot.wins}"
            state_str = slot.state
            if slot.last_reason and slot.state in ("cooldown", "restarting"):
                state_str = f"{slot.state} ({slot.last_reason})"
            print(f"  [{state_icon}] {name_pad} {deck:<8} {prog:>12}  {wins_col:<6} {state_str}{restarts}")
        print()
        active = sum(1 for s in self.slots if s.state in ("running", "starting"))
        done = sum(1 for s in self.slots if s.state == "done")
        total_wins = sum(s.wins for s in self.slots)

        completed = sum(
            int(s.progress.split("/")[0])
            for s in self.slots if "/" in s.progress
        )
        total_expected = len(self.slots) * self.games
        win_pct = f" ({100 * total_wins / completed:.1f}%)" if completed > 0 else ""

        elapsed = time.time() - self.session_start if self.session_start else 0
        if elapsed > 30 and completed > 0:
            rate = completed / (elapsed / 3600)
            remaining = total_expected - completed
            eta_secs = int(remaining / (rate / 3600))
            h, m = divmod(eta_secs // 60, 60)
            rate_str = f"  Rate: {rate:.0f} g/hr  ETA: ~{h}h{m:02d}m"
        else:
            rate_str = ""

        print(f"  Active: {active}  Done: {done}  Wins: {total_wins}{win_pct}  Session: {self.session_num}{rate_str}")
        print(f"  Ctrl+C to stop all")

    def run(self) -> None:
        setup_supervisor_logging()
        self.setup()
        self.session_start = time.time()
        log.info("=== Mother Gothel started: %d princesses, %d games each ===",
                 len(self.slots), self.games)
        names = ", ".join(s.name for s in self.slots)
        print(f"  Mother Gothel is sending {len(self.slots)} princesses to play Balatro")
        print(f"  ({names})\n")
        print(f"  Session {self.session_num}, {self.games} games each\n")

        for i, slot in enumerate(self.slots):
            if not self.running:
                break
            self.start_slot(slot)
            if i < len(self.slots) - 1:
                time.sleep(SERVER_STAGGER)

        last_status = 0.0
        while self.running:
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
                break

            time.sleep(1.0)

    def shutdown(self) -> None:
        if self._shutdown_done:
            return
        self._shutdown_done = True
        self.running = False
        log.info("Shutdown requested")
        print("\n  Shutting down...")
        for slot in self.slots:
            slot.kill_pair()
        # Sweep ports for any orphans that escaped process tree kill
        ports = [s.port for s in self.slots]
        kill_port_processes(ports)
        total_restarts = sum(s.restarts for s in self.slots)
        log.info("Shutdown complete. Total restarts: %d", total_restarts)
        if self.session_num > 0:
            self.post_run()
        print("  All princesses recalled.")

    def post_run(self) -> None:
        batch = f"{self.session_num:03d}"
        print(f"\n  Running stats for batch {batch}...")
        try:
            result = subprocess.run(
                [PYTHON, str(BASE_DIR / "stats.py"), batch],
                cwd=str(BASE_DIR),
                capture_output=False,
            )
            if result.returncode != 0:
                log.warning("stats.py exited with rc=%d", result.returncode)
        except Exception as e:
            log.warning("Failed to run stats.py: %s", e)
        self._rotate_logs()

    def _rotate_logs(self, keep: int = 10) -> None:
        archive_dir = LOG_DIR / "archive"
        archive_dir.mkdir(exist_ok=True)

        # Find all live batch numbers across port dirs
        batch_nums: set[int] = set()
        for port_dir in LOG_DIR.iterdir():
            if not port_dir.name.isdigit():
                continue
            for f in port_dir.glob("game_*.log"):
                m = re.match(r"game_(\d+)\.log", f.name)
                if m:
                    batch_nums.add(int(m.group(1)))

        to_archive = sorted(batch_nums, reverse=True)[keep:]
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
                for stem, prefix in (("game", "game"), ("scoring", "scoring")):
                    src = port_dir / f"{stem}_{n:03d}.log"
                    if src.exists():
                        shutil.move(str(src), str(dest / f"{prefix}_{port}_{n:03d}.log"))
                        moved += 1
                wins_src = WINS_DIR / f"wins_{port}_{n:03d}.log"
                if wins_src.exists():
                    shutil.move(str(wins_src), str(dest / wins_src.name))
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
    args = parser.parse_args()

    if args.kill:
        ports = list(range(BASE_PORT, BASE_PORT + args.instances))
        killed = kill_port_processes(ports)
        print(f"Killed {killed} processes")
        return

    decks = [d.strip() for d in args.deck.split(",")]
    sup = Supervisor(
        n=args.instances,
        games=args.games,
        decks=decks,
        stake=args.stake,
        seed=args.seed,
        verbose=args.verbose,
    )

    def on_sigint(sig, frame):
        sup.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, on_sigint)
    signal.signal(signal.SIGTERM, on_sigint)

    try:
        sup.run()
    finally:
        sup.shutdown()


if __name__ == "__main__":
    main()
