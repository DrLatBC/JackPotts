from __future__ import annotations

from pathlib import Path

from balatro_bot.instance_report import batch_numbers


def find_instance_dirs(log_dir: Path) -> list[Path]:
    """Find all instance directories (port numbers)."""
    return sorted(
        p for p in log_dir.iterdir()
        if p.is_dir() and p.name.isdigit()
    )


def find_latest_batch(log_dir: Path) -> str | None:
    """The highest batch number with a game log under *log_dir*."""
    numbers = batch_numbers(log_dir)
    return f"{max(numbers):03d}" if numbers else None
