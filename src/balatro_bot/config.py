"""Configuration for the supervisor and bot processes."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SupervisorConfig:
    base_port: int = 12346
    love_path: str = r"G:\SteamLibrary\steamapps\common\Balatro\Balatro.exe"
    lovely_path: str = r"G:\SteamLibrary\steamapps\common\Balatro\version.dll"
    uvx_path: str = r"C:\Users\Tyler\AppData\Roaming\Python\Python311\Scripts\uvx.exe"
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
