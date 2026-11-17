"""Stream launcher: the visible game on its own port, with a model playing it.

    .venv\\Scripts\\pythonw.exe -m balatro_bot.stream --model rl\\data\\ppo\\r1\\latest.pt --follow

Starts ``balatrobot serve`` with windows (not headless, not fast) on STREAM_PORT, in
that port's own profile with everything unlocked (``unlock_all``), then runs ``rl.stream`` (the model player, under .venv-rl)
against it. Both children sit in this process's kill-on-exit job, so if the launcher
dies the game closes with it. The control panel's Stream tab starts this.

Writes ``bot_log/model_stream/status.json`` (phase starting / running / stopping / stopped /
failed, with pids and settings); the player writes ``now.json`` beside it. Creating
``bot_log/model_stream/stop`` ends the stream: the player stops between moves, then the game
is closed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
import zipfile
import zlib
from pathlib import Path

from balatro_bot.files import replace_file
from balatro_bot.supervisor import (
    BALATROBOT, BASE_DIR, LOG_DIR, LOVE_PATH, LOVELY_PATH, ensure_isolated_profile, graceful_kill,
    kill_port_processes, tie_children_to_supervisor,
)

STREAM_PORT = 12340
STREAM_DIR = LOG_DIR / "model_stream"
STATUS = STREAM_DIR / "status.json"
STOP = STREAM_DIR / "stop"
PLAYER_LOG = STREAM_DIR / "stream.log"
GAME_LOG = STREAM_DIR / "game.log"
RL_PYTHON = BASE_DIR / ".venv-rl" / "Scripts" / "python.exe"
GAME_START_TIMEOUT = 120.0
STOP_TIMEOUT = 60.0


# The game's item tables; a key under meta.jkr's unlocked / discovered / alerted marks the
# item so when the game loads (game.lua, Game:init_item_prototypes).
GAME_TABLES = ("P_CENTERS", "P_BLINDS", "P_TAGS", "P_SEALS")
_KEY_BEFORE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*$")


def game_items(love_path: str) -> list[str]:
    """Every item key the installed game defines, read from its own game.lua; work in
    progress and demo-only items left out, as the game leaves them out."""
    with zipfile.ZipFile(love_path) as z:
        src = z.read("game.lua").decode("utf-8", "replace")
    keys = []
    for table in GAME_TABLES:
        head = f"self.{table} = {{"
        at = src.index(head) + len(head)
        depth, entry = 1, None
        while depth:
            ch = src[at]
            if ch == "{":
                depth += 1
                if depth == 2:
                    key = _KEY_BEFORE.search(src, max(0, at - 80), at)
                    entry = (key.group(1) if key else None, at)
            elif ch == "}":
                depth -= 1
                if depth == 1 and entry and entry[0]:
                    body = src[entry[1]:at]
                    if not re.search(r"\b(wip|demo)\s*=\s*true", body):
                        keys.append(entry[0])
                    entry = None
            at += 1
    return keys


def _read_jkr(path: Path) -> str:
    raw = path.read_bytes()
    return raw.decode("utf-8") if raw[:6] == b"return" else zlib.decompress(raw, -15).decode("utf-8")


def _write_jkr(path: Path, text: str) -> None:
    """Saved the way the game saves (compress_and_save: raw deflate)."""
    packer = zlib.compressobj(9, zlib.DEFLATED, -15)
    path.write_bytes(packer.compress(text.encode("utf-8")) + packer.flush())


def unlock_all(profile_dir: Path, love_path: str) -> None:
    """What the profile screen's "Unlock all" leaves on disk: every item unlocked,
    discovered and alerted in meta.jkr, and ``all_unlocked`` in profile.jkr. Jackdaw
    assumes this, so the stream's shadow only matches an unlocked profile."""
    keys = game_items(love_path)
    meta = "".join(f'["{part}"]={{' + "".join(f'["{k}"]=true,' for k in keys) + "},"
                   for part in ("alerted", "discovered", "unlocked"))
    profile_dir.mkdir(parents=True, exist_ok=True)
    _write_jkr(profile_dir / "meta.jkr", "return {" + meta + "}")
    profile = profile_dir / "profile.jkr"
    text = _read_jkr(profile) if profile.exists() else "return {}"
    if '["all_unlocked"]=true' not in text:
        text = text.replace('["all_unlocked"]=false,', "")
        text = text.replace("return {", 'return {["all_unlocked"]=true,', 1)
        _write_jkr(profile, text)


def write_status(status: dict, **changes) -> None:
    status.update(changes, pid=os.getpid(), updated=time.time())
    STREAM_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATUS.with_suffix(".tmp")
    tmp.write_text(json.dumps(status, indent=2), encoding="utf-8")
    replace_file(tmp, STATUS)


def listening(port: int) -> bool:
    try:
        with socket.create_connection(("localhost", port), timeout=2):
            return True
    except OSError:
        return False


def last_line(path: Path) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return next((ln.strip() for ln in reversed(lines) if ln.strip()), "")


def player_command(args) -> list[str]:
    cmd = [str(RL_PYTHON), "-m", "rl.stream", "--port", str(args.port), "--model", args.model,
           "--deck", args.deck, "--stake", args.stake, "--delay", str(args.delay)]
    return cmd + (["--follow"] if args.follow else [])


def run(args) -> int:
    status = {"phase": "starting", "model": args.model, "follow": args.follow, "deck": args.deck,
              "stake": args.stake, "delay": args.delay, "port": args.port, "started": time.time()}
    STOP.unlink(missing_ok=True)
    (STREAM_DIR / "now.json").unlink(missing_ok=True)
    write_status(status, doing="opening the game")
    tie_children_to_supervisor()
    kill_port_processes([args.port])
    env = os.environ.copy()
    env["APPDATA"] = ensure_isolated_profile(args.port)
    unlock_all(Path(env["APPDATA"]) / "Balatro" / "1", LOVE_PATH)
    flags = subprocess.CREATE_NO_WINDOW
    with open(GAME_LOG, "w", encoding="utf-8") as game_out:
        game = subprocess.Popen(
            [BALATROBOT, "serve", "--port", str(args.port), "--love-path", LOVE_PATH,
             "--lovely-path", LOVELY_PATH],
            env=env, stdout=game_out, stderr=subprocess.STDOUT, creationflags=flags,
        )
    player = None
    try:
        deadline = time.time() + GAME_START_TIMEOUT
        while not listening(args.port):
            if game.poll() is not None or time.time() > deadline or STOP.exists():
                why = ("stopped before the game opened" if STOP.exists()
                       else last_line(GAME_LOG) or "the game did not open")
                write_status(status, phase="stopped" if STOP.exists() else "failed", doing="", error=why)
                return 1
            time.sleep(1.0)
        with open(PLAYER_LOG, "w", encoding="utf-8") as player_out:
            player = subprocess.Popen(player_command(args), cwd=str(BASE_DIR), stdout=player_out,
                                      stderr=subprocess.STDOUT, creationflags=flags)
        write_status(status, phase="running", doing="", game_pid=game.pid, player_pid=player.pid)
        stop_seen = 0.0
        while True:
            if player.poll() is not None:
                if STOP.exists():
                    write_status(status, phase="stopped")
                    return 0
                write_status(status, phase="failed", error=last_line(PLAYER_LOG) or
                             f"the player exited with code {player.returncode}")
                return 1
            if game.poll() is not None:
                write_status(status, phase="stopped", note="the game window was closed")
                return 0
            if STOP.exists() and not stop_seen:
                stop_seen = time.time()
                write_status(status, phase="stopping")
            if stop_seen and time.time() - stop_seen > STOP_TIMEOUT:
                write_status(status, phase="stopped", note="the player was ended after the stop timed out")
                return 0
            time.sleep(1.0)
    finally:
        if player is not None and player.poll() is None:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(player.pid)], capture_output=True)
        graceful_kill(game)
        STOP.unlink(missing_ok=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--model", required=True)
    ap.add_argument("--follow", action="store_true", help="read the model again before each game")
    ap.add_argument("--deck", default="BLUE")
    ap.add_argument("--stake", default="WHITE")
    ap.add_argument("--delay", type=float, default=1.5)
    ap.add_argument("--port", type=int, default=STREAM_PORT)
    args = ap.parse_args()
    try:
        return run(args)
    except Exception as e:  # the panel reads the reason from the status file
        write_status({}, phase="failed", error=f"{type(e).__name__}: {e}")
        raise


if __name__ == "__main__":
    sys.exit(main())
