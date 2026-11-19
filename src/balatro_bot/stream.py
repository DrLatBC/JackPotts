"""Stream launcher: the visible game on its own port, with a model playing it.

    .venv\\Scripts\\pythonw.exe -m balatro_bot.stream --model rl\\data\\ppo\\r1\\latest.pt --follow

Starts the live game visible (``game_server.LiveGame``) on STREAM_PORT, in that port's
own profile with everything unlocked, then runs ``rl.stream`` (the model player, under
.venv-rl) against it. Both children sit in this process's kill-on-exit job, so if the launcher
dies the game closes with it. The control panel's Stream tab starts this.

Serves the overlay (``balatro_bot.overlay``) for OBS on OVERLAY_PORT while it runs.

The stream is a job (``balatro_bot.jobs``, folder ``bot_log/model_stream/``): this writes
its status (phase starting / running / stopping / stopped / failed, with pids and
settings) and the player writes ``now.json`` beside it. A stop asked ends the stream:
the player stops between moves, then the game is closed.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

from balatro_bot import game_server, jobs, overlay
from balatro_bot.config import REPO, venv_python

STREAM_PORT = 12340
GAME_START_TIMEOUT = 120.0
STOP_TIMEOUT = 60.0


def last_line(path: Path) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return next((ln.strip() for ln in reversed(lines) if ln.strip()), "")


def player_command(args) -> list[str]:
    cmd = [venv_python("rl"), "-m", "rl.stream", "--port", str(args.port), "--model", args.model,
           "--deck", args.deck, "--stake", args.stake, "--delay", str(args.delay)]
    return cmd + (["--follow"] if args.follow else [])


def run(args) -> int:
    job = jobs.stream()
    status = job.claim(model=args.model, follow=args.follow, deck=args.deck, stake=args.stake,
                       delay=args.delay, port=args.port)
    write_status = job.report
    player_log, game_log = job.file("stream.log"), job.file("game.log")
    job.file("now.json").unlink(missing_ok=True)
    try:
        overlay_server = overlay.serve(job.folder, args.overlay_port)
        status["overlay"] = f"http://localhost:{overlay_server.server_address[1]}/"
    except OSError as e:
        overlay_server = None
        status["overlay error"] = f"overlay port {args.overlay_port} unavailable: {e}"
    write_status(status, doing="opening the game")
    jobs.tie_children()
    jobs.kill_port_processes([args.port])
    flags = subprocess.CREATE_NO_WINDOW
    server = game_server.LiveGame(visible=True, profile="unlocked", log=game_log)
    game = server.start(args.port, flags)
    player = None
    try:
        deadline = time.time() + GAME_START_TIMEOUT
        while not server.health(args.port):
            if game.poll() is not None or time.time() > deadline or job.asked("stop"):
                why = ("stopped before the game opened" if job.asked("stop")
                       else last_line(game_log) or "the game did not open")
                write_status(status, phase="stopped" if job.asked("stop") else "failed", doing="", error=why)
                return 1
            time.sleep(1.0)
        with open(player_log, "w", encoding="utf-8") as player_out:
            player = subprocess.Popen(player_command(args), cwd=str(REPO), stdout=player_out,
                                      stderr=subprocess.STDOUT, creationflags=flags)
        write_status(status, phase="running", doing="", game_pid=game.pid, player_pid=player.pid)
        stop_seen = 0.0
        while True:
            if player.poll() is not None:
                if job.asked("stop"):
                    write_status(status, phase="stopped")
                    return 0
                write_status(status, phase="failed", error=last_line(player_log) or
                             f"the player exited with code {player.returncode}")
                return 1
            if game.poll() is not None:
                write_status(status, phase="stopped", note="the game window was closed")
                return 0
            if job.asked("stop") and not stop_seen:
                stop_seen = time.time()
                write_status(status, phase="stopping")
            if stop_seen and time.time() - stop_seen > STOP_TIMEOUT:
                write_status(status, phase="stopped", note="the player was ended after the stop timed out")
                return 0
            time.sleep(1.0)
    finally:
        if player is not None and player.poll() is None:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(player.pid)], capture_output=True)
        server.stop(game)
        job.clear()
        if overlay_server is not None:
            overlay_server.shutdown()
            overlay_server.server_close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--model", required=True)
    ap.add_argument("--follow", action="store_true", help="read the model again before each game")
    ap.add_argument("--deck", default="BLUE")
    ap.add_argument("--stake", default="WHITE")
    ap.add_argument("--delay", type=float, default=1.5)
    ap.add_argument("--port", type=int, default=STREAM_PORT)
    ap.add_argument("--overlay-port", type=int, default=overlay.OVERLAY_PORT)
    args = ap.parse_args()
    try:
        return run(args)
    except Exception as e:  # the panel reads the reason from the status file
        jobs.stream().fail(f"{type(e).__name__}: {e}")
        raise


if __name__ == "__main__":
    sys.exit(main())
