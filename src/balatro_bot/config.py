"""Configuration for the supervisor and bot processes: the repo's paths, the
interpreter each job runs on, and the game install. Standard library only: the bot
imports it on PyPy and the learning run from .venv-rl."""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
LOG_DIR = REPO / "bot_log"
WINS_DIR = LOG_DIR / "wins"
# Each live-game instance's own Balatro save folder (game_server.ensure_isolated_profile).
PROFILES_DIR = REPO / "profiles"


def _load_env_local() -> None:
    """Load .env.local from the repo root if it exists (no dependency needed)."""
    env_file = REPO / ".env.local"
    if not env_file.is_file():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key and key not in os.environ:
            os.environ[key] = value


_load_env_local()


# Each environment's folder under the repo, and the variable that overrides its interpreter.
_VENVS = {
    "bot": (".venv", None),
    "jackdaw": (".venv-jackdaw", "JACKDAW_PYTHON"),
    "pypy": (".venv-pypy", "BOT_PYPY"),
    "rl": (".venv-rl", None),
}


def venv_python(name: str, windowless: bool = False) -> str:
    """The interpreter of one of the repo's environments ("bot", "jackdaw", "pypy", "rl").

    The path is returned whether or not it exists; callers that can do without an
    environment check. The bot environment is the exception: a worktree checkout has
    no .venv of its own, so its jobs run on the interpreter running this.
    """
    folder, env_var = _VENVS[name]
    if env_var and os.environ.get(env_var):
        return os.environ[env_var]
    exe = "pythonw.exe" if windowless else "python.exe"
    path = REPO / folder / "Scripts" / exe
    if name == "bot" and not path.exists():
        here = Path(sys.executable)
        return str(here.with_name(exe) if windowless and here.with_name(exe).exists() else here)
    return str(path)


def _resolve_path(env_var: str, fallback: str, label: str) -> str:
    """Resolve a path from env var, with a fallback. Raises if neither exists."""
    path = os.environ.get(env_var, "")
    if path and os.path.exists(path):
        return path
    if os.path.exists(fallback):
        return fallback
    if path:
        # Env var was set but path doesn't exist
        raise FileNotFoundError(
            f"{label}: ${env_var} is set to '{path}' but the file doesn't exist."
        )
    raise FileNotFoundError(
        f"{label}: Set the {env_var} environment variable to your {label} path.\n"
        f"  Example (PowerShell): $env:{env_var} = \"C:\\path\\to\\{os.path.basename(fallback)}\""
    )


def _default_love_path() -> str:
    return _resolve_path(
        "BALATRO_EXE",
        r"C:\Program Files (x86)\Steam\steamapps\common\Balatro\Balatro.exe",
        "Balatro executable",
    )


def _default_lovely_path() -> str:
    return _resolve_path(
        "LOVELY_DLL",
        r"C:\Program Files (x86)\Steam\steamapps\common\Balatro\version.dll",
        "Lovely mod loader",
    )


def _default_balatrobot_path() -> str:
    """The balatrobot server CLI installed in this Python environment.

    The server must come from the local DrLatBC/balatrobot fork (installed
    editable), not PyPI: the Lua mod in Balatro/Mods is the fork's, and an
    unpinned PyPI server drifts ahead of it.
    """
    env = os.environ.get("BALATROBOT_EXE", "")
    if env:
        if os.path.exists(env):
            return env
        raise FileNotFoundError(
            f"balatrobot: $BALATROBOT_EXE is set to '{env}' but the file doesn't exist."
        )
    exe = "balatrobot.exe" if os.name == "nt" else "balatrobot"
    local = Path(sys.executable).parent / exe
    if local.exists():
        return str(local)
    found = shutil.which("balatrobot")
    if found:
        return found
    raise FileNotFoundError(
        "balatrobot: server CLI not found. Install the local fork into this "
        "environment:\n"
        "  pip install -e \"C:\\github vs\\balatrobot\"\n"
        "or set BALATROBOT_EXE to its balatrobot executable."
    )


@dataclass
class SupervisorConfig:
    base_port: int = 12346
    health_timeout: float = 30.0
    restart_cooldown: float = 30.0
    rapid_restart_window: float = 60.0
    rapid_restart_limit: int = 3
    server_startup_wait: float = 15.0
    server_stagger: float = 3.0
    bot_stagger: float = 2.0
    poll_interval: float = 5.0

    # Resolved on first use, so importing a module that holds a config (and the
    # simulator, which never starts the game) works on a machine without Balatro.
    @cached_property
    def love_path(self) -> str:
        return _default_love_path()

    @cached_property
    def lovely_path(self) -> str:
        return _default_lovely_path()

    @cached_property
    def balatrobot_path(self) -> str:
        return _default_balatrobot_path()
