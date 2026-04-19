"""Configuration for the supervisor and bot processes."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path


def _load_env_local() -> None:
    """Load .env.local from the repo root if it exists (no dependency needed)."""
    root = Path(__file__).resolve().parent.parent.parent
    env_file = root / ".env.local"
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


def _default_uvx_path() -> str:
    env = os.environ.get("UVX_PATH", "")
    if env:
        if os.path.exists(env):
            return env
        raise FileNotFoundError(
            f"uvx: $UVX_PATH is set to '{env}' but the file doesn't exist."
        )
    found = shutil.which("uvx")
    if found:
        return found
    raise FileNotFoundError(
        "uvx: not found on PATH. Install it (pip install uv) or set the UVX_PATH "
        "environment variable.\n"
        "  Example (PowerShell): $env:UVX_PATH = \"C:\\path\\to\\uvx.exe\""
    )


@dataclass
class SupervisorConfig:
    base_port: int = 12346
    love_path: str = field(default_factory=_default_love_path)
    lovely_path: str = field(default_factory=_default_lovely_path)
    uvx_path: str = field(default_factory=_default_uvx_path)
    python_path: str = ""
    base_dir: Path = field(default_factory=lambda: Path(__file__).parent.parent.parent)
    log_dir: Path = field(default_factory=lambda: Path(__file__).parent.parent.parent / "bot_log")
    wins_dir: Path = field(default_factory=lambda: Path(__file__).parent.parent.parent / "bot_log" / "wins")
    health_timeout: float = 30.0
    restart_cooldown: float = 30.0
    rapid_restart_window: float = 60.0
    rapid_restart_limit: int = 3
    server_startup_wait: float = 15.0
    server_stagger: float = 3.0
    bot_stagger: float = 2.0
    poll_interval: float = 5.0
