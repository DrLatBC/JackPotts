"""Files one process writes whole and others read while it runs (status files)."""

from __future__ import annotations

import os
import time
from pathlib import Path


def replace_file(tmp: Path, path: Path, tries: int = 40, wait: float = 0.05) -> bool:
    """Move a finished temp file over ``path``. Windows refuses while another process
    has ``path`` open (the control panel reading it), so this tries again for up to
    ``tries * wait`` seconds. False if it never got through; the temp file stays for
    the next write to replace."""
    for _ in range(tries):
        try:
            os.replace(tmp, path)
            return True
        except PermissionError:
            time.sleep(wait)
    return False
