"""A supervisor that dies without its shutdown takes its children with it (Windows job object)."""

import os
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows job objects")

# A stand-in supervisor: joins the job, starts a long-lived child, prints its pid, dies abruptly.
_PARENT = """
import os, subprocess, sys
from balatro_bot.supervisor import tie_children_to_supervisor
assert tie_children_to_supervisor()
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                         creationflags=subprocess.CREATE_NO_WINDOW)
print(child.pid, flush=True)
os._exit(1)
"""


def _alive(pid: int) -> bool:
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                         capture_output=True, text=True).stdout
    return str(pid) in out


def test_children_die_when_the_supervisor_dies_abruptly():
    src = os.path.join(os.path.dirname(__file__), "..", "src")
    parent = subprocess.run([sys.executable, "-c", _PARENT], capture_output=True, text=True,
                            env={**os.environ, "PYTHONPATH": os.path.abspath(src)}, timeout=60)
    child_pid = int(parent.stdout.split()[0])
    for _ in range(50):
        if not _alive(child_pid):
            break
        time.sleep(0.1)
    alive = _alive(child_pid)
    if alive:
        subprocess.run(["taskkill", "/F", "/PID", str(child_pid)], capture_output=True)
    assert not alive, "child outlived its supervisor"
