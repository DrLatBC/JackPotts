from __future__ import annotations

from pathlib import Path


def find_instance_dirs(log_dir: Path) -> list[Path]:
    """Find all instance directories (port numbers)."""
    return sorted(
        p for p in log_dir.iterdir()
        if p.is_dir() and p.name.isdigit()
    )


def find_latest_batch(log_dir: Path) -> str | None:
    """Find the highest batch number across all instances."""
    highest = 0
    for inst in find_instance_dirs(log_dir):
        for f in inst.glob("game_*.log"):
            num = int(f.stem.split("_")[1])
            highest = max(highest, num)
    return f"{highest:03d}" if highest > 0 else None
