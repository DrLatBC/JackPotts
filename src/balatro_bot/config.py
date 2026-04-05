"""Configuration for the supervisor and bot processes."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path


def _default_love_path() -> str:
    return os.environ.get(
        "BALATRO_EXE",
        r"C:\Program Files (x86)\Steam\steamapps\common\Balatro\Balatro.exe",
    )


def _default_lovely_path() -> str:
    return os.environ.get(
        "LOVELY_DLL",
        r"C:\Program Files (x86)\Steam\steamapps\common\Balatro\version.dll",
    )


def _default_uvx_path() -> str:
    return os.environ.get("UVX_PATH", "") or shutil.which("uvx") or "uvx"


@dataclass
class SupervisorConfig:
    base_port: int = 12346
    love_path: str = field(default_factory=_default_love_path)
    lovely_path: str = field(default_factory=_default_lovely_path)
    uvx_path: str = field(default_factory=_default_uvx_path)
    python_path: str = ""
    base_dir: Path = field(default_factory=lambda: Path(__file__).parent.parent.parent)
    log_dir: Path = field(default_factory=lambda: Path("bot_log"))
    wins_dir: Path = field(default_factory=lambda: Path("bot_log/wins"))
    health_timeout: float = 30.0
    restart_cooldown: float = 30.0
    rapid_restart_window: float = 60.0
    rapid_restart_limit: int = 3
    server_startup_wait: float = 15.0
    server_stagger: float = 3.0
    bot_stagger: float = 2.0
    poll_interval: float = 5.0
