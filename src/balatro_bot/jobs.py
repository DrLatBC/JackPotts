"""Jobs: the batch, learning runs and the stream, as the folders they share with the
control panel.

A job is a separate process that outlives the panel, so everything between them goes
through files. Each job has one folder under ``ROOT``:

    status.json   written by the job (``Job.report``), read by anyone (``Job.status``)
    stop, pause   requests (``Job.ask``), seen by every process of the job (``Job.asked``)
    commands/     a queue of JSON requests (``Job.send`` / ``Job.take``)
    console.log   the job's printed output when the panel launched it (``Job.spawn``)

Beside them sit the job's own files (``Job.file``). A job's launcher ties its children
to itself (``tie_children``) and clears its ports first (``kill_port_processes``).
Standard library and ``config`` only: the learning run and the stream's player import
this from ``.venv-rl``.
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from balatro_bot.config import LOG_DIR as ROOT
from balatro_bot.config import REPO

PHASES = ("starting", "running", "stopping", "pausing", "paused", "stopped", "finished", "failed")
LIVE = ("starting", "running", "stopping", "pausing")
REQUESTS = ("stop", "pause")
DIED = "The process ended without saving its status."

log = logging.getLogger("jobs")


class Busy(SystemExit):
    """Another living process holds the job."""

    def __init__(self, job: Job, pid: int) -> None:
        super().__init__(f"{job.name} is already running in process {pid}")
        self.pid = pid


def pid_alive(pid: int) -> bool:
    """True while a process with this pid runs."""
    if pid <= 0:
        return False
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    query_limited_information, still_active = 0x1000, 259
    kernel = ctypes.windll.kernel32
    handle = kernel.OpenProcess(query_limited_information, False, pid)
    if not handle:
        return False
    code = ctypes.c_ulong()
    ok = kernel.GetExitCodeProcess(handle, ctypes.byref(code))
    kernel.CloseHandle(handle)
    return bool(ok) and code.value == still_active


def replace_file(tmp: Path, path: Path, tries: int = 40, wait: float = 0.05) -> bool:
    """Move a finished temp file over ``path``. Windows refuses while another process
    has ``path`` open (the panel reading it), so this tries again for up to
    ``tries * wait`` seconds. False if it never got through; the temp file stays for
    the next write to replace."""
    for _ in range(tries):
        try:
            os.replace(tmp, path)
            return True
        except PermissionError:
            time.sleep(wait)
    return False


def write_text(path: Path, text: str) -> None:
    """Swap ``text`` in as the whole of ``path``, so a reader never sees half of it.
    PermissionError when another process kept ``path`` open throughout."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    if not replace_file(tmp, path):
        raise PermissionError(f"{path} stayed open elsewhere; the new copy is in {tmp}")


def write_json(path: Path, obj, indent: int | None = 2) -> None:
    write_text(path, json.dumps(obj, indent=indent))


_LAST_GOOD: dict[Path, dict] = {}


def read_json(path: Path) -> dict | None:
    """A JSON file another process rewrites while this one reads. None once it is gone;
    the last good copy when this read lands on a write (Windows refuses the read while
    the writer swaps the file in)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        _LAST_GOOD.pop(path, None)
        return None
    except (OSError, ValueError):
        return _LAST_GOOD.get(path)
    _LAST_GOOD[path] = data
    return data


class Job:
    """One job's folder. ``kind`` is "batch", "learning run" or "stream"."""

    def __init__(self, folder: Path, kind: str) -> None:
        self.folder = Path(folder)
        self.kind = kind

    @property
    def name(self) -> str:
        return self.folder.name if self.kind == "learning run" else self.kind

    def file(self, name: str) -> Path:
        return self.folder / name

    # -- status -------------------------------------------------------------------

    def status(self) -> dict | None:
        """The job's status as it stands: None with no status file; ``failed`` when it
        says it is live but its process is gone; ``stopping`` / ``pausing`` while it
        runs with that request waiting."""
        status = read_json(self.file("status.json"))
        if status is None:
            return None
        phase = status.get("phase")
        if phase in LIVE and not pid_alive(int(status.get("pid") or 0)):
            return {**status, "phase": "failed", "error": status.get("error") or DIED}
        if phase == "running":
            for request, shown in (("stop", "stopping"), ("pause", "pausing")):
                if self.asked(request):
                    return {**status, "phase": shown}
        return status

    def live(self) -> bool:
        status = self.status()
        return status is not None and status.get("phase") in LIVE

    def report(self, status: dict, **changes) -> None:
        """Merge the changes into ``status`` and write it out. A write that doesn't get
        through is left for the next one: the status only feeds the panel, so it never
        ends the job."""
        status.update(changes, job=self.kind, pid=os.getpid(), updated=time.time())
        try:
            write_json(self.file("status.json"), status)
        except OSError as e:
            log.warning("Could not write %s: %s", self.file("status.json"), e)

    def claim(self, **fields) -> dict:
        """Mark the job as this process's (phase ``starting``) and return its status to
        keep reporting into. Raises Busy when another living process holds it: two
        processes on one job overwrite each other's files. Waiting requests are cleared;
        they were meant for an earlier process."""
        current = read_json(self.file("status.json")) or {}
        pid = int(current.get("pid") or 0)
        if current.get("phase") in LIVE and pid != os.getpid() and pid_alive(pid):
            raise Busy(self, pid)
        self.clear()
        status = {"phase": "starting", "started": time.time(), **fields}
        self.report(status)
        return status

    def fail(self, error: str) -> None:
        """Mark the job failed if this process holds it; a refusal before the claim
        leaves the status of whoever held it last."""
        status = read_json(self.file("status.json")) or {}
        if status.get("pid") == os.getpid():
            self.report(status, phase="failed", doing="", error=error)
            self.clear()

    # -- requests -----------------------------------------------------------------

    def ask(self, request: str) -> None:
        """Ask the job to stop or pause; it acts between steps."""
        assert request in REQUESTS
        self.folder.mkdir(parents=True, exist_ok=True)
        self.file(request).touch()

    def asked(self, request: str) -> bool:
        return self.file(request).exists()

    def clear(self) -> None:
        """Drop waiting requests: the job calls this when it claims and as it exits."""
        for request in REQUESTS:
            self.file(request).unlink(missing_ok=True)

    def send(self, command: dict) -> None:
        """Queue a request for the running job's next tick."""
        write_json(self.file("commands") / f"{time.time_ns()}.json", command, indent=None)

    def take(self) -> list[dict]:
        """Every queued request, oldest first, removed from the queue. An unreadable
        one comes back empty."""
        out = []
        for path in sorted(self.file("commands").glob("*.json")):
            try:
                out.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                out.append({})
            path.unlink(missing_ok=True)
        return out

    # -- launching ----------------------------------------------------------------

    def spawn(self, cmd: list[str], append: bool = False) -> subprocess.Popen:
        """Start the job windowless in its own process group, printing into
        ``console.log``; it outlives the panel."""
        self.folder.mkdir(parents=True, exist_ok=True)
        self.clear()
        flags = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
                 if sys.platform == "win32" else 0)
        with open(self.file("console.log"), "a" if append else "w", encoding="utf-8") as out:
            return subprocess.Popen([str(c) for c in cmd], cwd=str(REPO), stdout=out,
                                    stderr=subprocess.STDOUT, creationflags=flags)

    def console_tail(self) -> str:
        """The last line the job printed."""
        try:
            lines = self.file("console.log").read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return ""
        return next((ln.strip() for ln in reversed(lines) if ln.strip()), "")


# -- processes ----------------------------------------------------------------------

def kill_port_processes(ports: list[int]) -> int:
    """Force-kill whatever listens on or is connected to these ports; the count killed."""
    killed = 0
    try:
        result = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True
        )
        pids_to_kill: set[str] = set()
        for line in result.stdout.splitlines():
            for port in ports:
                if f":{port}" in line and ("LISTENING" in line or "ESTABLISHED" in line):
                    parts = line.split()
                    pid = parts[-1]
                    if pid.isdigit() and pid != "0":
                        pids_to_kill.add(pid)
        for pid in pids_to_kill:
            subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
            killed += 1
    except Exception:
        pass
    return killed


class _BasicLimits(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32)]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BasicLimits), ("IoInfo", ctypes.c_uint64 * 6),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_job_handle = None  # held open for this process's lifetime; Windows closes it at exit


def tie_children() -> bool:
    """Put this process in a Windows job that ends every process in it when this one exits.

    Children (game servers, Balatro, bots, players) join the job when they start, so a
    batch or stream launcher that dies without its shutdown (killed, crashed, its
    console closed) takes them with it instead of leaving them running.
    """
    global _job_handle
    if os.name != "nt":
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    kernel32.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        log.warning("could not create a job object (error %d); children may outlive this process",
                    ctypes.get_last_error())
        return False
    info = _ExtendedLimits()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not (kernel32.SetInformationJobObject(job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                                             ctypes.byref(info), ctypes.sizeof(info))
            and kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess())):
        log.warning("could not join a kill-on-exit job (error %d); children may outlive this process",
                    ctypes.get_last_error())
        kernel32.CloseHandle(ctypes.c_void_p(job))
        return False
    _job_handle = job
    return True


def batch() -> Job:
    return Job(ROOT / "batch", "batch")


def learning_run(name: str) -> Job:
    return Job(ROOT / "training" / name, "learning run")


def learning_runs() -> list[Job]:
    """Every learning run with a status."""
    folder = ROOT / "training"
    return [learning_run(p.parent.name) for p in sorted(folder.glob("*/status.json"))]


def stream() -> Job:
    return Job(ROOT / "model_stream", "stream")
