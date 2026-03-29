"""
Supervisor for Balatro bot instances.

Manages N (balatrobot serve + bot.py) pairs with health monitoring,
automatic restart, and clean shutdown.

Usage:
    python supervisor.py              # 6 instances, 1000 games each
    python supervisor.py -n 8         # 8 instances
    python supervisor.py --kill       # kill all existing processes and exit
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import json
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_PORT = 12346
BASE_DIR = Path(__file__).parent
BOT_SCRIPT = BASE_DIR / "bot.py"
LOG_DIR = BASE_DIR / "bot_log"
WINS_DIR = LOG_DIR / "wins"

LOVE_PATH = r"G:\SteamLibrary\steamapps\common\Balatro\Balatro.exe"
LOVELY_PATH = r"G:\SteamLibrary\steamapps\common\Balatro\version.dll"
UVX = r"C:\Users\Tyler\AppData\Roaming\Python\Python311\Scripts\uvx.exe"
PYTHON = str(BASE_DIR / ".venv" / "Scripts" / "python.exe")

HEALTH_TIMEOUT = 30  # seconds before declaring server dead
RESTART_COOLDOWN = 30  # seconds to wait after 3+ rapid restarts
RAPID_RESTART_WINDOW = 60  # seconds window for counting rapid restarts
RAPID_RESTART_LIMIT = 3
SERVER_STARTUP_WAIT = 15  # seconds to wait for server after launch
SERVER_STAGGER = 3  # seconds between server launches
BOT_STAGGER = 2  # seconds between bot launches
POLL_INTERVAL = 5  # seconds between monitor ticks

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
# Slot
# ---------------------------------------------------------------------------

@dataclass
class Slot:
    port: int
    server_proc: subprocess.Popen | None = None
    bot_proc: subprocess.Popen | None = None
    state: str = "stopped"  # stopped, starting, running, restarting, done, cooldown
    restarts: int = 0
    restart_times: list[float] = field(default_factory=list)
    last_health: float = 0.0
    progress: str = ""

    def is_alive(self, proc: subprocess.Popen | None) -> bool:
        return proc is not None and proc.poll() is None

    def kill_pair(self) -> None:
        """Kill both server and bot processes."""
        for proc in (self.bot_proc, self.server_proc):
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                except OSError:
                    pass
        # Give them a moment, then force kill
        deadline = time.time() + 3.0
        for proc in (self.bot_proc, self.server_proc):
            if proc and proc.poll() is None:
                remaining = max(0, deadline - time.time())
                try:
                    proc.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    try:
                        proc.kill()
                    except OSError:
                        pass
        self.server_proc = None
        self.bot_proc = None

    def check_health(self) -> bool:
        """Check if the balatrobot server is accepting connections."""
        try:
            with socket.create_connection(("localhost", self.port), timeout=2):
                self.last_health = time.time()
                return True
        except (OSError, TimeoutError):
            pass
        return False

    def read_progress(self) -> str:
        """Read progress from the bot's progress.txt."""
        try:
            p = LOG_DIR / str(self.port) / "progress.txt"
            if p.exists():
                self.progress = p.read_text().strip()
        except OSError:
            pass
        return self.progress


# ---------------------------------------------------------------------------
# Process cleanup
# ---------------------------------------------------------------------------

def kill_port_processes(ports: list[int]) -> int:
    """Kill all processes listening on the given ports. Returns count killed."""
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
    """Find the next available session number across all port log dirs."""
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
    def __init__(self, n: int, games: int, deck: str, stake: str,
                 seed: str | None):
        self.slots = [Slot(port=BASE_PORT + i) for i in range(n)]
        self.games = games
        self.deck = deck
        self.stake = stake
        self.seed = seed
        self.running = True
        self.session_num = 0

    def setup(self) -> None:
        """Create log dirs, compute session number, kill orphans."""
        WINS_DIR.mkdir(parents=True, exist_ok=True)
        for slot in self.slots:
            (LOG_DIR / str(slot.port)).mkdir(parents=True, exist_ok=True)

        # Kill anything on our ports
        ports = [s.port for s in self.slots]
        killed = kill_port_processes(ports)
        if killed:
            print(f"Cleaned up {killed} orphan processes")
            time.sleep(2)

        self.session_num = compute_session_number()
        # Write session number for bot.py to read
        (LOG_DIR / "next_num.txt").write_text(str(self.session_num))

    def launch_server(self, slot: Slot) -> None:
        """Start a balatrobot serve process for this slot."""
        slot.server_proc = subprocess.Popen(
            [UVX, "balatrobot", "serve",
             "--port", str(slot.port),
             "--headless", "--fast",
             "--love-path", LOVE_PATH,
             "--lovely-path", LOVELY_PATH],
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        slot.state = "starting"
        slot.last_health = time.time()  # grace period starts now

    def launch_bot(self, slot: Slot) -> None:
        """Start a bot.py process for this slot."""
        cmd = [
            PYTHON, str(BOT_SCRIPT),
            "--start", "--port", str(slot.port),
            "--games", str(self.games),
            "--uvx", UVX,
            "--love-path", LOVE_PATH,
            "--lovely-path", LOVELY_PATH,
            "--deck", self.deck,
            "--stake", self.stake,
        ]
        if self.seed:
            cmd.extend(["--seed", self.seed])
        slot.bot_proc = subprocess.Popen(
            cmd,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        slot.state = "running"

    def wait_for_health(self, slot: Slot, timeout: float = 30.0) -> bool:
        """Block until the server's health endpoint responds."""
        deadline = time.time() + timeout
        while time.time() < deadline and self.running:
            if slot.check_health():
                return True
            if not slot.is_alive(slot.server_proc):
                return False
            time.sleep(1.0)
        return False

    def start_slot(self, slot: Slot) -> None:
        """Launch server + bot for a slot."""
        self.launch_server(slot)
        log.info("Port %d: server starting (pid=%s)", slot.port,
                 slot.server_proc.pid if slot.server_proc else "?")
        if self.wait_for_health(slot, timeout=SERVER_STARTUP_WAIT):
            self.launch_bot(slot)
            log.info("Port %d: bot started (pid=%s)", slot.port,
                     slot.bot_proc.pid if slot.bot_proc else "?")
        else:
            log.warning("Port %d: server slow to start, launching bot anyway", slot.port)
            self.launch_bot(slot)
            slot.last_health = time.time()

    def restart_slot(self, slot: Slot, reason: str = "unknown") -> None:
        """Kill and relaunch a slot."""
        now = time.time()
        # Track restart times for backoff
        slot.restart_times = [t for t in slot.restart_times
                              if now - t < RAPID_RESTART_WINDOW]
        if len(slot.restart_times) >= RAPID_RESTART_LIMIT:
            slot.state = "cooldown"
            log.warning("Port %d: cooldown — %d restarts in %ds (last: %s)",
                        slot.port, len(slot.restart_times), RAPID_RESTART_WINDOW, reason)
            return

        slot.restart_times.append(now)
        slot.restarts += 1
        slot.state = "restarting"
        log.warning("Port %d: restart #%d — %s", slot.port, slot.restarts, reason)

        slot.kill_pair()
        time.sleep(2)
        self.start_slot(slot)

    def check_slot(self, slot: Slot) -> None:
        """Check a single slot's health and restart if needed."""
        if slot.state == "done" or slot.state == "stopped":
            return

        if slot.state == "cooldown":
            latest = max(slot.restart_times) if slot.restart_times else 0
            if time.time() - latest > RESTART_COOLDOWN:
                self.restart_slot(slot)
            return

        # Check if bot exited cleanly (finished all games)
        if slot.bot_proc and slot.bot_proc.poll() is not None:
            rc = slot.bot_proc.returncode
            if rc == 0:
                slot.state = "done"
                slot.read_progress()
                log.info("Port %d: finished (%s)", slot.port, slot.progress)
                slot.kill_pair()
                return
            else:
                log.error("Port %d: bot exited rc=%d", slot.port, rc)
                self.restart_slot(slot, reason=f"bot exited rc={rc}")
                return

        # Check if server process died
        if not slot.is_alive(slot.server_proc):
            server_rc = slot.server_proc.returncode if slot.server_proc else "?"
            log.error("Port %d: server died rc=%s", slot.port, server_rc)
            self.restart_slot(slot, reason=f"server died rc={server_rc}")
            return

    def print_status(self) -> None:
        """Print a status table to the console."""
        os.system("cls" if os.name == "nt" else "clear")
        print("  Balatro Bot Supervisor")
        print("  " + "=" * 50)
        print()
        for slot in self.slots:
            slot.read_progress()
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
            print(f"  [{state_icon}] Port {slot.port}: {prog:>12}  {slot.state}{restarts}")
        print()
        active = sum(1 for s in self.slots if s.state in ("running", "starting"))
        done = sum(1 for s in self.slots if s.state == "done")
        print(f"  Active: {active}  Done: {done}  Session: {self.session_num}")
        print(f"  Ctrl+C to stop all")

    def run(self) -> None:
        """Main supervisor loop."""
        setup_supervisor_logging()
        self.setup()
        log.info("=== Supervisor started: %d instances, %d games each ===",
                 len(self.slots), self.games)

        print(f"Starting {len(self.slots)} instances (session {self.session_num})...\n")

        # Staggered startup
        for i, slot in enumerate(self.slots):
            if not self.running:
                break
            self.start_slot(slot)
            if i < len(self.slots) - 1:
                time.sleep(SERVER_STAGGER)

        # Monitor loop
        last_status = 0.0
        while self.running:
            for slot in self.slots:
                self.check_slot(slot)

            if time.time() - last_status > POLL_INTERVAL:
                self.print_status()
                last_status = time.time()

            # Exit if all slots are finished or permanently failed
            active_states = {"starting", "running", "restarting", "cooldown"}
            if not any(s.state in active_states for s in self.slots):
                self.print_status()
                done = sum(1 for s in self.slots if s.state == "done")
                print(f"\n  All instances stopped ({done} finished).")
                break

            time.sleep(1.0)

    def shutdown(self) -> None:
        """Clean shutdown of all slots."""
        self.running = False
        log.info("Shutdown requested")
        print("\n  Shutting down...")
        for slot in self.slots:
            slot.kill_pair()
        total_restarts = sum(s.restarts for s in self.slots)
        log.info("Shutdown complete. Total restarts: %d", total_restarts)
        print("  All processes terminated.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Balatro bot supervisor")
    parser.add_argument("-n", "--instances", type=int, default=6,
                        help="Number of bot instances (default: 6)")
    parser.add_argument("--games", type=int, default=1000,
                        help="Games per instance (default: 1000)")
    parser.add_argument("--deck", default="RED", help="Deck type")
    parser.add_argument("--stake", default="WHITE", help="Stake level")
    parser.add_argument("--seed", default=None, help="Game seed")
    parser.add_argument("--kill", action="store_true",
                        help="Kill all existing bot/balatro processes and exit")
    args = parser.parse_args()

    if args.kill:
        ports = list(range(BASE_PORT, BASE_PORT + args.instances))
        killed = kill_port_processes(ports)
        print(f"Killed {killed} processes")
        return

    sup = Supervisor(
        n=args.instances,
        games=args.games,
        deck=args.deck,
        stake=args.stake,
        seed=args.seed,
    )

    # Handle Ctrl+C gracefully
    def on_sigint(sig, frame):
        sup.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, on_sigint)

    try:
        sup.run()
    finally:
        sup.shutdown()


if __name__ == "__main__":
    main()
