"""Game servers: what a bot plays against on a port (see GLOSSARY.md).

Two adapters, the live game (``LiveGame``: Balatro running the balatrobot mod) and the
simulator (``Jackdaw``: ``sim/jackdaw_adapter/serve.py``). Each knows how to start and
stop its server, whether it is up, which interpreter its bots run on, how long a bot
may go without acting before it counts as stuck, and the tag its batches' code version
carries. Whether a server's responses arrive settled is the server's to say, in its
``health`` reply; ``read_health`` reads it. The supervisor and the stream start servers
through this; the bot reads ``read_health``. Standard library and ``config`` only.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import socket
import subprocess
import time
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, Literal

from balatro_bot.config import PROFILES_DIR, REPO, SupervisorConfig, venv_python

log = logging.getLogger("game_server")

# The game install (Balatro, Lovely, the balatrobot fork's server), resolved on first use.
INSTALL = SupervisorConfig()
JACKDAW_ADAPTER = REPO / "sim" / "jackdaw_adapter" / "serve.py"
REAL_APPDATA = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
REAL_BALATRO_DIR = REAL_APPDATA / "Balatro"
GRACEFUL_EXIT_TIMEOUT = 8.0  # seconds to wait for Balatro to finish saving on shutdown


def ensure_isolated_profile(port: int) -> str:
    """Create a per-port APPDATA override and return its path.

    Layout created:
        profiles/<port>/Balatro/Mods      -> junction to real %APPDATA%/Balatro/Mods
        profiles/<port>/Balatro/config    -> junction to real %APPDATA%/Balatro/config (if exists)
        profiles/<port>/Balatro/settings.jkr   (copied once from real)
        profiles/<port>/Balatro/1/meta.jkr     (copied once from real)
        profiles/<port>/Balatro/1/profile.jkr  (copied once from real)
        profiles/<port>/Balatro/1/save.jkr     (copied once from real)

    Mods is a junction so mod updates propagate; .jkr files are real copies
    so each instance writes independently.
    """
    port_dir = PROFILES_DIR / str(port)
    balatro_dir = port_dir / "Balatro"
    balatro_dir.mkdir(parents=True, exist_ok=True)

    # Junction Mods/ (and config/) to the real install — these are shared.
    for shared in ("Mods", "config"):
        link = balatro_dir / shared
        target = REAL_BALATRO_DIR / shared
        if not target.exists():
            continue
        if link.exists() or link.is_symlink():
            continue
        # mklink /J creates a directory junction (no admin required on Windows)
        try:
            subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                capture_output=True, check=True,
            )
        except (OSError, subprocess.CalledProcessError) as e:
            log.warning("Could not junction %s -> %s: %s", link, target, e)

    # Seed the save files once — copies tutorial-done state, unlocks, etc.
    for name in ("settings.jkr",):
        src = REAL_BALATRO_DIR / name
        dst = balatro_dir / name
        if src.exists() and not dst.exists():
            shutil.copy2(src, dst)

    profile_sub = balatro_dir / "1"
    profile_sub.mkdir(exist_ok=True)
    for name in ("meta.jkr", "profile.jkr", "save.jkr"):
        src = REAL_BALATRO_DIR / "1" / name
        dst = profile_sub / name
        if src.exists() and not dst.exists():
            shutil.copy2(src, dst)

    return str(port_dir)


def graceful_kill(proc: subprocess.Popen, timeout: float = GRACEFUL_EXIT_TIMEOUT) -> None:
    """Ask the process tree to close, then force-kill after `timeout`.

    Prevents corrupting Balatro's .jkr save files by force-killing mid-write.
    `taskkill /T` (without /F) sends WM_CLOSE to GUI processes, letting
    Love2D run its quit handler and finish any pending save.
    """
    if proc.poll() is not None:
        return
    try:
        subprocess.run(
            ["taskkill", "/T", "/PID", str(proc.pid)],
            capture_output=True,
        )
    except OSError:
        pass

    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return
        time.sleep(0.25)

    # Timed out — force-kill
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
        )
    except OSError:
        pass


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


def listening(port: int) -> bool:
    """True when something accepts connections on the port: the server is up."""
    try:
        with socket.create_connection(("localhost", port), timeout=2):
            return True
    except OSError:
        return False


@dataclass(frozen=True)
class Health:
    """What a server's ``health`` reply says about it."""

    # Every response is the settled game state. The live game answers before money,
    # joker counters and debuff flags settle, so a bot re-polls before deciding there.
    settled: bool
    # Name and version of the simulator ("jackdaw 0.2.6"), "" on the live game.
    simulator: str


def read_health(reply) -> Health:
    simulator = reply.get("simulator") if isinstance(reply, dict) else None
    if not simulator:
        return Health(settled=False, simulator="")
    return Health(settled=True, simulator=f"{simulator} {reply.get('simulator_version') or ''}".strip())


@dataclass(frozen=True)
class LiveGame:
    """Balatro with the balatrobot mod, served by the fork's ``balatrobot serve``.

    Hidden it runs headless and fast (a batch); visible it is the normal window at
    normal speed. Its save folder is the port's own copy of the real profile, or with
    ``profile="unlocked"`` everything unlocked, as Jackdaw assumes. With ``log`` the
    server's output goes to that file.
    """

    visible: bool = False
    profile: Literal["copied", "unlocked"] = "copied"
    log: Path | None = None

    name: ClassVar[str] = "live"
    stall_timeout: ClassVar[float] = 600.0
    version_tag: ClassVar[str] = ""

    def command(self, port: int) -> list[str]:
        cmd = [INSTALL.balatrobot_path, "serve", "--port", str(port),
               "--love-path", INSTALL.love_path, "--lovely-path", INSTALL.lovely_path]
        return cmd if self.visible else cmd + ["--headless", "--fast"]

    def environment(self, port: int) -> dict[str, str]:
        """The server's env: APPDATA points Love2D at the port's own save folder, since
        instances writing one profile at once corrupt it."""
        env = os.environ.copy()
        env["APPDATA"] = ensure_isolated_profile(port)
        if self.profile == "unlocked":
            unlock_all(Path(env["APPDATA"]) / "Balatro" / "1", INSTALL.love_path)
        return env

    def start(self, port: int, creationflags: int = 0) -> subprocess.Popen:
        cmd, env = self.command(port), self.environment(port)
        if self.log is None:
            return subprocess.Popen(cmd, env=env, creationflags=creationflags)
        with open(self.log, "w", encoding="utf-8") as out:
            return subprocess.Popen(cmd, env=env, stdout=out, stderr=subprocess.STDOUT,
                                    creationflags=creationflags)

    def stop(self, proc: subprocess.Popen) -> None:
        graceful_kill(proc)

    def health(self, port: int) -> bool:
        return listening(port)

    def bot_python(self) -> str:
        return venv_python("bot")


@dataclass(frozen=True)
class Jackdaw:
    """The Jackdaw simulator behind balatrobot's API (``sim/jackdaw_adapter``), in
    ``.venv-jackdaw`` or ``$JACKDAW_PYTHON``."""

    python: str = field(default_factory=lambda: venv_python("jackdaw"))

    name: ClassVar[str] = "jackdaw"
    # A simulator game never goes a minute without an action.
    stall_timeout: ClassVar[float] = 60.0
    version_tag: ClassVar[str] = "+jackdaw"

    def available(self) -> bool:
        return os.path.exists(self.python)

    def command(self, port: int) -> list[str]:
        return [self.python, "-I", str(JACKDAW_ADAPTER), "--port", str(port)]

    def start(self, port: int, creationflags: int = 0) -> subprocess.Popen:
        return subprocess.Popen(self.command(port), creationflags=creationflags)

    def stop(self, proc: subprocess.Popen) -> None:
        if proc.poll() is None:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)

    def health(self, port: int) -> bool:
        return listening(port)

    def bot_python(self) -> str:
        return venv_python("bot")


GameServer = LiveGame | Jackdaw
