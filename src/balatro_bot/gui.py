"""Desktop control panel for batch runs: start, watch and stop the supervisor.

The supervisor runs as its own windowless process, so closing this window
never stops a batch; reopening it picks the running batch back up from the
status file the supervisor writes every tick. Stop asks for a clean
shutdown through the supervisor's stop file. Exploration is switched per
instance by rewriting the rate file its bot re-reads before each game.

Launch with the repo's .venv pythonw (no console):
    .venv\\Scripts\\pythonw.exe -m balatro_bot.gui [--start]
--start also starts a batch with the saved settings, as the Start button
would. scripts\\start_batch.vbs runs that with no window at all, for
launching from another program such as a Claude session.
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import time
import tkinter as tk
import webbrowser
from ctypes import wintypes
from pathlib import Path
from tkinter import font as tkfont
from tkinter import messagebox, ttk

from balatro_bot import learning
from balatro_bot.learning import pid_alive
from balatro_bot.supervisor import (
    BASE_DIR, BASE_PORT, COMMANDS_DIR, LOG_DIR, MAX_INSTANCES, PAUSE_FILE, PORT_SPAN, PYTHON,
    STATUS_FILE, STOP_FILE,
)

ICON_FILE = Path(__file__).parent / "assets" / "jackpotts.ico"
# Without its own app ID, Windows groups the window under pythonw.exe and shows Python's icon.
APP_ID = "DrLatBC.JackPotts.ControlPanel"
SETTINGS_FILE = LOG_DIR / "gui_settings.json"
CONSOLE_LOG = LOG_DIR / "supervisor_console.log"
DASHBOARD_URL = "https://jackpotts.drlat.dev/dashboard"
REFRESH_MS = 2000

# The balatrobot mod's start endpoint accepts exactly these.
DECKS = [
    "RED", "BLUE", "YELLOW", "GREEN", "BLACK", "MAGIC", "NEBULA", "GHOST",
    "ABANDONED", "CHECKERED", "ZODIAC", "PAINTED", "ANAGLYPH", "PLASMA", "ERRATIC",
]
STAKES = ["WHITE", "RED", "GREEN", "BLACK", "BLUE", "PURPLE", "ORANGE", "GOLD"]

DEFAULTS = {"instances": 6, "games": 2000, "deck": "BLUE", "stake": "WHITE",
            "explore_instances": 2, "explore_rate": 0.15, "sim": False, "kernel": False,
            "train_run": "", "train_copy": "b187", "train_hours": 1.0, "train_beta": 0.1,
            "stream_source": "", "stream_checkpoint": "latest", "stream_deck": "BLUE",
            "stream_stake": "WHITE", "stream_delay": 1.5}


# Dashboard palette
BG = "#1a1a2e"
CARD = "#1f2b47"
CARD_HOVER = "#26345a"
BORDER = "#2a3550"
TEXT = "#e0e0e0"
MUTED = "#8892a4"
DIM = "#5a6478"
GOLD = "#d4a843"
GREEN = "#4caf50"
RED = "#e74c3c"
BLUE = "#3498db"
PURPLE = "#9b59b6"
PURPLE_FILL = "#33284d"
YELLOW = "#f1c40f"

STATE_COLOURS = {
    "running": GREEN, "starting": BLUE, "restarting": YELLOW,
    "cooldown": RED, "dead": RED, "done": MUTED, "stopped": DIM,
}
PHASE_COLOURS = {"running": GREEN, "stopping": YELLOW, "pausing": YELLOW, "paused": GOLD, "starting": BLUE,
                 "outside": PURPLE, "idle": BORDER}
TRAIN_COLOURS = {"idle": DIM, "starting": BLUE, "running": GREEN, "stopping": YELLOW,
                 "stopped": MUTED, "finished": GOLD, "failed": RED, "outside": PURPLE}

FONT = "Segoe UI"
FULL_MIN = (1060, 660)  # the full view's smallest size, at 96 dpi

# Screen pixels per 96-dpi pixel; set once the window exists. Sizes below are
# written at 96 dpi and pass through px() so the panel stays sharp and in
# proportion on scaled displays.
_scale = 1.0


def px(n: float) -> int:
    return int(round(n * _scale))


def read_status_file() -> dict | None:
    """The status file as last written, whether or not its supervisor is alive."""
    try:
        return json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def read_status() -> dict | None:
    """The supervisor's latest status, or None when no supervisor is alive."""
    status = read_status_file()
    if status is None or status.get("phase") == "stopped" or not pid_alive(int(status.get("pid", 0))):
        return None
    return status


def listening_ports(netstat_output: str) -> set[int]:
    """Local ports in LISTENING state from `netstat -ano -p TCP` output."""
    ports = set()
    for line in netstat_output.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[0] == "TCP" and parts[3] == "LISTENING":
            port = parts[1].rsplit(":", 1)[-1]
            if port.isdigit():
                ports.add(int(port))
    return ports


def ports_in_use(count: int = PORT_SPAN) -> bool:
    """True when any instance port is taken, e.g. by a batch started from a terminal.

    Starting a batch kills whatever holds those ports, so Start stays off.
    Reads the listening table rather than connecting: the mod serves one
    client per port and drops the bot's connection for any newcomer.
    """
    out = subprocess.run(
        ["netstat", "-ano", "-p", "TCP"],
        capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW,
    ).stdout
    return bool(listening_ports(out) & set(range(BASE_PORT, BASE_PORT + count)))


def set_explore_rate(port: int, rate: float) -> None:
    """Set an instance's explore rate; its bot switches at the start of its next game."""
    path = LOG_DIR / str(port) / "explore_rate.txt"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(f"{round(rate, 2)}")
    os.replace(tmp, path)


def send_command(command: dict) -> None:
    """Queue an add/remove request; the running supervisor carries it out on its next tick."""
    COMMANDS_DIR.mkdir(parents=True, exist_ok=True)
    path = COMMANDS_DIR / f"{time.time_ns()}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(command), encoding="utf-8")
    os.replace(tmp, path)


def paused_status(status_file: dict | None) -> dict | None:
    """The status file when it holds a paused batch waiting to resume."""
    if status_file and status_file.get("phase") == "paused" and "resume" in status_file:
        return status_file
    return None


def load_settings() -> dict:
    try:
        saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    return {**DEFAULTS, **{k: v for k, v in saved.items() if k in DEFAULTS}}


def save_settings(changes: dict) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps({**load_settings(), **changes}), encoding="utf-8")


def launch_batch(settings: dict) -> None:
    """Start the supervisor windowless with these settings and remember them for the panel."""
    save_settings(settings)
    STOP_FILE.unlink(missing_ok=True)
    STATUS_FILE.unlink(missing_ok=True)
    _spawn_supervisor(supervisor_command(settings))


def resume_paused_batch(finish: bool = False) -> None:
    """Carry on the paused batch, or with finish, end it (post its finish and run stats)."""
    STOP_FILE.unlink(missing_ok=True)
    _spawn_supervisor([PYTHON, "-m", "balatro_bot.supervisor", "--finish-paused" if finish else "--resume"])


def _spawn_supervisor(cmd: list[str]) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONSOLE_LOG, "w", encoding="utf-8") as out:
        subprocess.Popen(
            cmd,
            cwd=str(BASE_DIR),
            stdout=out,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
        )


def start_saved_batch() -> bool:
    """Start a batch with the saved settings unless one is running or paused; True if started."""
    if read_status() is not None or paused_status(read_status_file()) or ports_in_use():
        return False
    launch_batch(load_settings())
    return True


def supervisor_command(settings: dict) -> list[str]:
    cmd = [
        PYTHON, "-m", "balatro_bot.supervisor",
        "-n", str(settings["instances"]),
        "--games", str(settings["games"]),
        "--deck", settings["deck"],
        "--stake", settings["stake"],
    ]
    if settings["explore_instances"] > 0:
        cmd += ["--explore-instances", str(settings["explore_instances"]),
                "--explore-rate", str(settings["explore_rate"])]
    if settings.get("sim"):
        cmd.append("--sim")
    if settings.get("kernel"):
        cmd.append("--kernel")
    return cmd


def batch_active() -> bool:
    """True while a batch is playing, from this panel or a terminal."""
    return read_status() is not None or ports_in_use()


def batch_summary(status: dict) -> str:
    completed, wins = status.get("completed", 0), status.get("wins", 0)
    pct = f"  {_win_pct(wins, completed)}" if completed else ""
    return f"{completed:,} / {status.get('total_expected', 0):,} games  ·  {wins:,} wins{pct}"


def _fmt_eta(secs: int | None) -> str:
    if secs is None:
        return "—"
    h, m = divmod(secs // 60, 60)
    return f"~{h}h{m:02d}m"


def _fmt_secs(secs: float) -> str:
    secs = int(secs)
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    return f"{secs // 3600}h"


def _fmt_rate(per_minute: float) -> str:
    """Games per minute: whole numbers from ten up, one decimal below."""
    return f"{per_minute:.0f}" if per_minute >= 10 else f"{per_minute:.1f}"


def _win_pct(wins: int, games: int) -> str:
    return f"{100 * wins / games:.1f}%" if games else ""


# ── Tray icon ──

_WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class _WndClass(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT), ("lpfnWndProc", _WNDPROC), ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR),
    ]


class _NotifyIconData(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256), ("uTimeout", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64), ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", wintypes.HICON),
    ]


_user32, _shell32 = ctypes.windll.user32, ctypes.windll.shell32
_user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_user32.DefWindowProcW.restype = ctypes.c_ssize_t
_user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
_user32.CreateWindowExW.restype = wintypes.HWND
_user32.FindWindowW.restype = wintypes.HWND
_user32.RegisterClassW.argtypes = [ctypes.POINTER(_WndClass)]
_user32.RegisterClassW.restype = wintypes.ATOM
ctypes.windll.kernel32.GetModuleHandleW.restype = wintypes.HMODULE
_user32.LoadImageW.restype = wintypes.HANDLE
_user32.CreatePopupMenu.restype = wintypes.HMENU
_user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
_user32.TrackPopupMenu.argtypes = [
    wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.LPVOID]
_user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]

TRAY_CLASS = "JackPottsTray"
WM_TRAY = 0x8001  # WM_APP + 1: clicks on the tray icon
WM_SHOW_PANEL = 0x8002  # WM_APP + 2: a second launch asking this panel to come forward
_MENU_OPEN, _MENU_QUIT = 1, 2


def show_running_panel() -> bool:
    """Bring forward a panel that is already running; False when there is none."""
    hwnd = _user32.FindWindowW(TRAY_CLASS, None)
    return bool(hwnd) and bool(_user32.PostMessageW(hwnd, WM_SHOW_PANEL, 0, 0))


class TrayIcon:
    """The panel's tray icon: click to open, right-click for Open and Quit, and batch notifications.

    Its hidden window lives on Tk's thread, so Tk's event loop delivers the
    clicks and the callbacks can touch Tk directly.
    """

    def __init__(self, on_open, on_quit) -> None:
        self.on_open, self.on_quit = on_open, on_quit
        self._proc = _WNDPROC(self._wndproc)  # held so ctypes doesn't free the callback
        self._taskbar_created = _user32.RegisterWindowMessageW("TaskbarCreated")
        wc = _WndClass(lpfnWndProc=self._proc, lpszClassName=TRAY_CLASS,
                       hInstance=ctypes.windll.kernel32.GetModuleHandleW(None))
        _user32.RegisterClassW(ctypes.byref(wc))
        # A plain hidden window rather than a message-only one: the tray menu
        # needs a window that can take the foreground to close on outside clicks.
        self.hwnd = _user32.CreateWindowExW(0, TRAY_CLASS, "JackPotts", 0, 0, 0, 0, 0,
                                            None, None, wc.hInstance, None)
        self.data = _NotifyIconData(cbSize=ctypes.sizeof(_NotifyIconData), hWnd=self.hwnd, uID=1,
                                    uFlags=0x1 | 0x2 | 0x4,  # NIF_MESSAGE | NIF_ICON | NIF_TIP
                                    uCallbackMessage=WM_TRAY, szTip="JackPotts")
        self.data.hIcon = _user32.LoadImageW(None, str(ICON_FILE), 1, 0, 0, 0x10 | 0x40)  # file, default size
        _shell32.Shell_NotifyIconW(0, ctypes.byref(self.data))  # NIM_ADD

    def notify(self, title: str, message: str) -> None:
        self.data.uFlags |= 0x10  # NIF_INFO
        self.data.szInfoTitle = title[:63]
        self.data.szInfo = message[:255]
        self.data.dwInfoFlags = 0x1  # NIIF_INFO
        _shell32.Shell_NotifyIconW(1, ctypes.byref(self.data))  # NIM_MODIFY
        self.data.uFlags &= ~0x10

    def close(self) -> None:
        _shell32.Shell_NotifyIconW(2, ctypes.byref(self.data))  # NIM_DELETE
        _user32.DestroyWindow(self.hwnd)

    def _menu(self) -> None:
        menu = _user32.CreatePopupMenu()
        _user32.AppendMenuW(menu, 0, _MENU_OPEN, "Open")
        _user32.AppendMenuW(menu, 0, _MENU_QUIT, "Quit panel")
        pt = wintypes.POINT()
        _user32.GetCursorPos(ctypes.byref(pt))
        _user32.SetForegroundWindow(self.hwnd)
        choice = _user32.TrackPopupMenu(menu, 0x100 | 0x2, pt.x, pt.y, 0, self.hwnd, None)  # RETURNCMD | RIGHTBUTTON
        _user32.PostMessageW(self.hwnd, 0, 0, 0)  # WM_NULL, so the menu closes properly next time
        _user32.DestroyMenu(menu)
        if choice == _MENU_OPEN:
            self.on_open()
        elif choice == _MENU_QUIT:
            self.on_quit()

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_TRAY:
            event = lparam & 0xFFFF
            if event == 0x202:  # WM_LBUTTONUP
                self.on_open()
            elif event == 0x205:  # WM_RBUTTONUP
                self._menu()
            return 0
        if msg == WM_SHOW_PANEL:
            self.on_open()
            return 0
        if msg == self._taskbar_created:  # Explorer restarted and dropped the icon
            _shell32.Shell_NotifyIconW(0, ctypes.byref(self.data))
            return 0
        return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)


# ── Widgets ──

class Tooltip:
    def __init__(self, root: tk.Misc) -> None:
        self.win = tk.Toplevel(root)
        self.win.withdraw()
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.label = tk.Label(self.win, bg=BORDER, fg=TEXT, font=(FONT, 9), padx=px(8), pady=px(4),
                              justify="left", wraplength=px(360))
        self.label.pack()
        self.text = ""

    def show(self, text: str, x: int, y: int) -> None:
        if not text:
            self.hide()
            return
        if text != self.text:
            self.label.configure(text=text)
            self.text = text
        self.win.geometry(f"+{x + px(14)}+{y + px(16)}")
        self.win.deiconify()
        self.win.lift()

    def hide(self) -> None:
        self.text = ""
        self.win.withdraw()


class InstanceTable(tk.Canvas):
    """One row per instance, drawn so each cell can carry its own colour and bar."""

    # key, heading, width at 96 dpi (the 0-width column takes what's left)
    COLUMNS = [
        ("dot", "", 24), ("name", "NAME", 86), ("deck", "DECK", 112), ("explore", "EXPLORE", 80),
        ("progress", "PROGRESS", 146), ("wins", "WINS", 80), ("speed", "GAMES/MIN", 92),
        ("idle", "IDLE", 46), ("issue", "LAST ISSUE", 0), ("remove", "", 28),
    ]

    def __init__(self, master: tk.Misc, tooltip: Tooltip, on_explore, on_remove, **kw) -> None:
        super().__init__(master, bg=CARD, highlightthickness=0, bd=0, **kw)
        self.head_h, self.row_h = px(26), px(32)
        self.tooltip = tooltip
        self.on_explore = on_explore
        self.on_remove = on_remove
        self.status: dict | None = None
        self.empty_text = "No batch running"
        self.hover_row: int | None = None
        self.first = 0  # index of the top visible row; the mouse wheel scrolls past one screen of rows
        self.bold = tkfont.Font(family=FONT, size=10, weight="bold")
        self.head_font = tkfont.Font(family=FONT, size=8, weight="bold")
        self.small = tkfont.Font(family=FONT, size=9)
        self.bind("<Configure>", lambda e: self.redraw())
        self.bind("<Motion>", self._motion)
        self.bind("<Leave>", self._leave)
        self.bind("<Button-1>", self._click)
        self.bind("<Double-Button-1>", self._double)
        self.bind("<MouseWheel>", self._wheel)

    def update_status(self, status: dict | None) -> None:
        self.status = status
        self.redraw()

    @property
    def slots(self) -> list[dict]:
        """Rows to show: removed instances drop out, their games stay in the batch totals."""
        return [s for s in self.status["slots"] if s["state"] != "removed"] if self.status else []

    def _spans(self) -> dict[str, tuple[int, int]]:
        x, spans = px(8), {}
        fixed = sum(px(w) for _, _, w in self.COLUMNS)
        for key, _, w in self.COLUMNS:
            w = px(w) or max(px(60), self.winfo_width() - fixed - px(16))
            spans[key] = (x, x + w)
            x += w
        return spans

    def _hit(self, x: int, y: int) -> tuple[int | None, str | None]:
        if y < self.head_h:
            return None, None
        row = (y - self.head_h) // self.row_h + self.first
        if row >= len(self.slots):
            return None, None
        for key, (x0, x1) in self._spans().items():
            if x0 <= x < x1:
                return row, key
        return row, None

    def _visible(self) -> int:
        return max(1, (self.winfo_height() - self.head_h) // self.row_h)

    def _wheel(self, e: tk.Event) -> None:
        self.first -= 3 * (1 if e.delta > 0 else -1)
        self.hover_row = None
        self.tooltip.hide()
        self.redraw()

    def explore_toggleable(self) -> bool:
        """Only a supervisor that hands its bots a rate file can switch them mid-batch."""
        return bool(self.status and self.status.get("phase") == "running"
                    and self.slots and "explore_active" in self.slots[0])

    def removable(self, slot: dict) -> bool:
        """Any instance still playing can go, except the last one: that is what Stop is for."""
        live = [s for s in self.slots if s["state"] != "done"]
        return bool(self.status and self.status.get("phase") == "running"
                    and slot["state"] != "done" and len(live) > 1)

    # ── Events ──

    def _tip_for(self, slot: dict, key: str | None) -> str:
        if key == "dot":
            return slot["state"]
        if key == "explore":
            if not self.explore_toggleable():
                return ""
            pending = slot["explore_rate"] != slot["explore_active"]
            return "Switches at the next game" if pending else "Click to switch at the next game"
        if key == "issue" and slot["last_reason"]:
            return f"{slot['restarts']} restarts · last: {slot['last_reason']}"
        if key == "name" and slot.get("log"):
            return "Double-click to open the game log"
        if key == "remove" and self.removable(slot):
            return "Remove this instance"
        return ""

    def _motion(self, e: tk.Event) -> None:
        row, key = self._hit(e.x, e.y)
        if row != self.hover_row:
            self.hover_row = row
            self.redraw()
        clickable = row is not None and (key == "explore" and self.explore_toggleable()
                                         or key == "remove" and self.removable(self.slots[row]))
        self.configure(cursor="hand2" if clickable else "")
        tip = self._tip_for(self.slots[row], key) if row is not None else ""
        self.tooltip.show(tip, e.x_root, e.y_root)

    def _leave(self, _e: tk.Event) -> None:
        self.hover_row = None
        self.tooltip.hide()
        self.redraw()

    def _click(self, e: tk.Event) -> None:
        row, key = self._hit(e.x, e.y)
        if row is not None and key == "explore" and self.explore_toggleable():
            self.on_explore(self.slots[row])
        elif row is not None and key == "remove" and self.removable(self.slots[row]):
            self.on_remove(self.slots[row])

    def _double(self, e: tk.Event) -> None:
        row, key = self._hit(e.x, e.y)
        if row is None or key in ("explore", "remove"):
            return
        log_path = self.slots[row].get("log")
        if log_path and Path(log_path).exists():
            os.startfile(log_path)

    # ── Drawing ──

    @staticmethod
    def _fit(text: str, width: int, f: tkfont.Font) -> str:
        if f.measure(text) <= width:
            return text
        while text and f.measure(text + "…") > width:
            text = text[:-1]
        return text + "…"

    def redraw(self) -> None:
        self.delete("all")
        spans = self._spans()
        width = self.winfo_width()
        for key, title, _ in self.COLUMNS:
            self.create_text(spans[key][0], self.head_h // 2 + px(2), text=title, anchor="w",
                             fill=MUTED, font=self.head_font)
        self.create_line(px(8), self.head_h, width - px(8), self.head_h, fill=BORDER)
        if not self.slots:
            self.create_text(width // 2, self.head_h + px(60), text=self.empty_text, fill=DIM,
                             font=(FONT, 11))
            return
        slots, visible = self.slots, self._visible()
        self.first = max(0, min(self.first, len(slots) - visible))
        last = min(len(slots), self.first + visible)
        if len(slots) > visible:
            self.create_text(width - px(12), self.head_h // 2 + px(2), anchor="e", fill=MUTED,
                             font=self.small, text=f"{self.first + 1}–{last} of {len(slots)}")
        stall = (self.status or {}).get("stall_secs", 600)
        for i in range(self.first, last):
            self._draw_row(i, slots[i], spans, stall)

    def _draw_row(self, i: int, s: dict, spans: dict, stall: int) -> None:
        top = self.head_h + (i - self.first) * self.row_h
        mid = top + self.row_h // 2
        width = self.winfo_width()
        if i == self.hover_row:
            self.create_rectangle(px(4), top + 1, width - px(4), top + self.row_h - 1,
                                  fill=CARD_HOVER, width=0)
        self.create_line(px(8), top + self.row_h, width - px(8), top + self.row_h, fill=BORDER)
        x = {k: v[0] for k, v in spans.items()}
        w = {k: v[1] - v[0] - px(10) for k, v in spans.items()}

        r = px(5)
        self.create_oval(x["dot"] + px(4), mid - r, x["dot"] + px(4) + 2 * r, mid + r,
                         fill=STATE_COLOURS.get(s["state"], MUTED), outline="")

        active = s.get("explore_active", s["explore_rate"])
        self.create_text(x["name"], mid, text=self._fit(s["name"], w["name"], self.bold), anchor="w",
                         fill=PURPLE if active else TEXT, font=self.bold)
        self.create_text(x["deck"], mid, text=s["deck"], anchor="w", fill=MUTED, font=self.small)
        if s.get("stake"):
            self.create_text(x["deck"] + self.small.measure(s["deck"]) + px(6), mid, text=s["stake"],
                             anchor="w", fill=DIM, font=self.small)

        self._draw_explore(x["explore"], mid, s["explore_rate"], active)

        done = s.get("games_completed", 0)
        total = (self.status or {}).get("games_per_instance") or 0
        bar_w, bar_h = px(64), px(3)
        self.create_rectangle(x["progress"], mid - bar_h, x["progress"] + bar_w, mid + bar_h,
                              fill=BORDER, width=0)
        if total and done:
            fill_w = max(px(2), int(bar_w * min(done / total, 1)))
            self.create_rectangle(x["progress"], mid - bar_h, x["progress"] + fill_w, mid + bar_h,
                                  fill=GOLD, width=0)
        self.create_text(x["progress"] + bar_w + px(8), mid, text=s["progress"] or "…", anchor="w",
                         fill=MUTED, font=self.small)

        wins = s["wins"]
        self.create_text(x["wins"], mid, text=str(wins), anchor="w", fill=GOLD if wins else DIM,
                         font=self.bold)
        if wins:
            self.create_text(x["wins"] + self.bold.measure(str(wins)) + px(6), mid,
                             text=_win_pct(wins, done), anchor="w", fill=MUTED, font=self.small)

        speed = s.get("games_per_min")
        if speed is None:
            self.create_text(x["speed"], mid, text="—", anchor="w", fill=DIM, font=self.small)
        else:
            self.create_text(x["speed"], mid, text=_fmt_rate(speed), anchor="w", fill=TEXT, font=self.bold)

        idle = s.get("idle_secs")
        if idle is not None:
            idle_colour = RED if idle >= stall * 0.8 else YELLOW if idle >= stall * 0.4 else MUTED
            self.create_text(x["idle"], mid, text=_fmt_secs(idle), anchor="w", fill=idle_colour,
                             font=self.small)

        ix = x["issue"]
        if s["restarts"]:
            label = f"↻{s['restarts']}"
            self.create_text(ix, mid, text=label, anchor="w", fill=YELLOW, font=self.small)
            ix += self.small.measure(label) + px(8)
        if s["last_reason"]:
            room = spans["issue"][1] - ix - px(4)
            self.create_text(ix, mid, text=self._fit(s["last_reason"], room, self.small), anchor="w",
                             fill=MUTED, font=self.small)

        if self.removable(s):
            self.create_text(x["remove"] + px(8), mid, text="✕", anchor="w",
                             fill=RED if i == self.hover_row else DIM, font=self.small)

    def _draw_explore(self, x: int, mid: int, rate: float, active: float) -> None:
        """Filled pill: exploring now. Dashed: switching at the next game. Plain: greedy."""
        on, pending = rate > 0, rate != active
        x1, h = x + px(62), px(10)
        if pending:
            self.create_rectangle(x, mid - h, x1, mid + h, outline=PURPLE if on else MUTED, dash=(2, 2))
            fg = PURPLE if on else MUTED
        elif on:
            self.create_rectangle(x, mid - h, x1, mid + h, fill=PURPLE_FILL, outline=PURPLE)
            fg = PURPLE
        else:
            self.create_rectangle(x, mid - h, x1, mid + h, outline=BORDER)
            fg = DIM
        self.create_text((x + x1) // 2, mid, text=f"◈ {rate * 100:.0f}%" if on else "off", fill=fg,
                         font=self.small)


class RecentGames(tk.Canvas):
    """The last few hundred finished games in one picture: how many won, the ante
    the rest ended at, and the latest wins by name and seed."""

    LOWEST_TOP_ANTE = 8

    def __init__(self, master: tk.Misc, tooltip: Tooltip, **kw) -> None:
        super().__init__(master, bg=CARD, highlightthickness=0, bd=0, **kw)
        self.head_h, self.row_h, self.bar_h = px(26), px(26), px(19)
        self.tooltip = tooltip
        self.recent: dict = {}
        self.wins: list[dict] = []
        # (top, bottom, tooltip text) per hoverable row, set by redraw.
        self.hover: list[tuple[int, int, str]] = []
        self.font = tkfont.Font(family=FONT, size=10)
        self.bold = tkfont.Font(family=FONT, size=10, weight="bold")
        self.small = tkfont.Font(family=FONT, size=9)
        self.bind("<Configure>", lambda e: self.redraw())
        self.bind("<Motion>", self._motion)
        self.bind("<Leave>", lambda e: self.tooltip.hide())

    def update_summary(self, recent: dict | None, wins: list[dict] | None) -> None:
        self.recent, self.wins = recent or {}, wins or []
        self.redraw()

    def _motion(self, e: tk.Event) -> None:
        for top, bottom, text in self.hover:
            if top <= e.y < bottom:
                self.tooltip.show(text, e.x_root, e.y_root)
                return
        self.tooltip.hide()

    def _heading(self, top: int, text: str) -> int:
        """A section heading with its rule; returns the y below it."""
        width = self.winfo_width()
        self.create_text(px(12), top + self.head_h // 2 + px(2), text=text, anchor="w", fill=MUTED,
                         font=(FONT, 8, "bold"))
        self.create_line(px(8), top + self.head_h, width - px(8), top + self.head_h, fill=BORDER)
        return top + self.head_h

    def redraw(self) -> None:
        self.delete("all")
        self.hover = []
        width, height = self.winfo_width(), self.winfo_height()
        games = self.recent.get("games", 0)
        y = self._heading(0, f"LAST {games:,} GAMES" if games else "RECENT GAMES")
        if not games:
            self.create_text(width // 2, y + px(34), text="None yet", fill=DIM, font=(FONT, 10))
            return

        wins = self.recent.get("wins", 0)
        mid = y + self.row_h // 2
        r = px(4)
        self.create_oval(px(14), mid - r, px(14) + 2 * r, mid + r, fill=GOLD if wins else DIM, outline="")
        self.create_text(px(32), mid, text=f"{wins:,} won", anchor="w", fill=GOLD if wins else MUTED,
                         font=self.bold if wins else self.font)
        self.create_text(width - px(12), mid, text=_win_pct(wins, games), anchor="e", fill=MUTED,
                         font=self.small)
        y += self.row_h

        # One bar per ante a game ended at, deepest first; the longest bar fills the row.
        lost_at = {int(ante): count for ante, count in self.recent.get("lost_at", {}).items()}
        most = max(lost_at.values(), default=0)
        left, right = px(40), width - px(46)
        for ante in range(max(self.LOWEST_TOP_ANTE, *lost_at, 0), 0, -1):
            count = lost_at.get(ante, 0)
            mid = y + self.bar_h // 2
            self.create_text(px(14), mid, text=f"A{ante}", anchor="w", fill=MUTED if count else DIM,
                             font=self.small)
            if count:
                length = max(px(2), round((right - left) * count / most))
                self.create_rectangle(left, mid - px(4), left + length, mid + px(4), fill=MUTED, width=0)
            self.create_text(width - px(12), mid, text=f"{count:,}" if count else "", anchor="e",
                             fill=TEXT, font=self.small)
            self.hover.append((y, y + self.bar_h, f"{count:,} of {games:,} ended at ante {ante}"))
            y += self.bar_h

        y = self._heading(y + px(4), "LATEST WINS")
        if not self.wins:
            self.create_text(width // 2, y + px(24), text="None yet", fill=DIM, font=(FONT, 10))
            return
        now = time.time()
        for g in self.wins:
            if y + self.row_h > height:
                break
            mid = y + self.row_h // 2
            self.create_text(px(14), mid, text=g["name"], anchor="w", fill=GOLD, font=self.bold)
            self.create_text(width - px(44), mid, text=g["seed"], anchor="e", fill=MUTED, font=self.small)
            self.create_text(width - px(12), mid, text=_fmt_secs(now - g["time"]), anchor="e",
                             fill=DIM, font=self.small)
            self.hover.append((y, y + self.row_h, f"{g['name']} won · seed {g['seed']}"))
            y += self.row_h


# ── Learning runs and the stream ──


def _blend(a: str, b: str, t: float) -> str:
    """The colour ``t`` of the way from ``a`` to ``b``."""
    ca = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    cb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(ca, cb))


def _pct(v: float) -> str:
    return f"{v:.1%}"


def _num(digits: int):
    return lambda v: f"{round(v, digits) + 0.0:.{digits}f}"  # + 0.0 turns -0.0 into 0.0


class LineChart(tk.Canvas):
    """One measure by update: a gold line (dots when there are few points), a recessive
    grid, the latest value in the corner, and a crosshair with the value on hover.
    ``band`` shades a ± range around each point."""

    def __init__(self, master: tk.Misc, tooltip: Tooltip, title: str, fmt, **kw) -> None:
        super().__init__(master, bg=CARD, highlightthickness=0, bd=0, **kw)
        self.tooltip, self.title, self.fmt = tooltip, title, fmt
        self.xs: list[float] = []
        self.ys: list[float] = []
        self.band: list[float] | None = None
        self._pts: list[tuple[float, float]] = []
        self._plot = (0, 0)
        self.bind("<Configure>", lambda _e: self.redraw())
        self.bind("<Motion>", self._motion)
        self.bind("<Leave>", self._leave)

    def set(self, xs, ys, band=None) -> None:
        keep = [i for i, (x, y) in enumerate(zip(xs, ys)) if x is not None and y is not None]
        new = ([xs[i] for i in keep], [ys[i] for i in keep], None if band is None else [band[i] for i in keep])
        if new != (self.xs, self.ys, self.band):
            self.xs, self.ys, self.band = new
            self.redraw()

    def redraw(self) -> None:
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        self._pts = []
        if w < px(60) or h < px(60):
            return
        self.create_text(px(12), px(13), text=self.title, anchor="w", fill=MUTED, font=(FONT, 8, "bold"))
        if not self.ys:
            self.create_text(w / 2, h / 2 + px(6), text="No data yet", fill=DIM, font=(FONT, 9))
            return
        right_pad, top, bottom = px(14), px(30), h - px(20)
        self.create_text(w - right_pad, px(13), text=self.fmt(self.ys[-1]), anchor="e", fill=TEXT,
                         font=(FONT, 10, "bold"))
        band = self.band or [0.0] * len(self.ys)
        lo = min(y - b for y, b in zip(self.ys, band))
        hi = max(y + b for y, b in zip(self.ys, band))
        if hi - lo < 1e-12:
            hi, lo = hi + max(abs(hi) * 0.05, 1e-6), lo - max(abs(lo) * 0.05, 1e-6)
        margin = (hi - lo) * 0.08
        lo, hi = lo - margin, hi + margin
        label_w = max(tkfont.Font(family=FONT, size=7).measure(self.fmt(v)) for v in (lo, hi, (lo + hi) / 2))
        left, right = px(16) + label_w, w - right_pad
        x0, x1 = min(self.xs), max(self.xs)
        span_x = (x1 - x0) or 1

        def X(x):
            return left + (x - x0) / span_x * (right - left) if x1 > x0 else (left + right) / 2

        def Y(y):
            return bottom - (y - lo) / (hi - lo) * (bottom - top)

        for frac in (0.0, 0.5, 1.0):
            value = lo + (hi - lo) * frac
            self.create_line(left, Y(value), right, Y(value), fill=BORDER)
            self.create_text(left - px(6), Y(value), text=self.fmt(value), anchor="e", fill=DIM,
                             font=(FONT, 7))
        self.create_text(left, bottom + px(4), text=f"u{x0:g}", anchor="nw", fill=DIM, font=(FONT, 7))
        if x1 > x0:
            self.create_text(right, bottom + px(4), text=f"u{x1:g}", anchor="ne", fill=DIM, font=(FONT, 7))
        pts = [(X(x), Y(y)) for x, y in zip(self.xs, self.ys)]
        if self.band:
            shade = _blend(CARD, GOLD, 0.25)
            upper = [(X(x), Y(y + b)) for x, y, b in zip(self.xs, self.ys, self.band)]
            lower = [(X(x), Y(y - b)) for x, y, b in zip(self.xs, self.ys, self.band)]
            if len(pts) > 1:
                self.create_polygon(*[c for p in upper + lower[::-1] for c in p], fill=shade, outline="")
            else:
                self.create_line(upper[0][0], upper[0][1], lower[0][0], lower[0][1], fill=shade, width=px(6))
        if len(pts) > 1:
            self.create_line(*[c for p in pts for c in p], fill=GOLD, width=px(2), capstyle="round",
                             joinstyle="round")
        if len(pts) <= 30:
            r = px(4)
            for x, y in pts:
                self.create_oval(x - r, y - r, x + r, y + r, fill=GOLD, outline=CARD, width=px(2))
        self._pts, self._plot = pts, (top, bottom)

    def _motion(self, e: tk.Event) -> None:
        self.delete("hover")
        if not self._pts:
            return
        i = min(range(len(self._pts)), key=lambda k: abs(self._pts[k][0] - e.x))
        x, y = self._pts[i]
        top, bottom = self._plot
        self.create_line(x, top, x, bottom, fill=MUTED, dash=(2, 3), tags="hover")
        r = px(5)
        self.create_oval(x - r, y - r, x + r, y + r, fill=GOLD, outline=TEXT, width=1, tags="hover")
        text = f"Update {self.xs[i]:g}: {self.fmt(self.ys[i])}"
        if self.band:
            text += f" ± {self.fmt(self.band[i])}"
        self.tooltip.show(text, e.x_root, e.y_root)

    def _leave(self, _e: tk.Event) -> None:
        self.delete("hover")
        self.tooltip.hide()


class RunList(tk.Canvas):
    """The learning runs, playing first, then the most recently active. The top row
    starts a new one."""

    ROW = 50

    def __init__(self, master: tk.Misc, on_select, **kw) -> None:
        super().__init__(master, bg=CARD, highlightthickness=0, bd=0, **kw)
        self.on_select = on_select
        self.rows: list[dict] = []
        self.selected: str | None = None
        self.offset = 0
        self.hover = -1
        self.bind("<Configure>", lambda _e: self.redraw())
        self.bind("<Button-1>", self._click)
        self.bind("<MouseWheel>", self._wheel)
        self.bind("<Motion>", self._motion)
        self.bind("<Leave>", self._motion)

    def update_rows(self, rows: list[dict], selected: str | None) -> None:
        self.rows, self.selected = rows, selected
        self.redraw()

    def _index(self, y: int) -> int:
        return int((y + self.offset) // px(self.ROW))

    def _wheel(self, e: tk.Event) -> None:
        most = max(0, (len(self.rows) + 1) * px(self.ROW) - self.winfo_height())
        self.offset = max(0, min(most, self.offset - int(e.delta / 120) * px(self.ROW)))
        self.redraw()

    def _motion(self, e: tk.Event) -> None:
        inside = 0 <= e.x < self.winfo_width() and 0 <= e.y < self.winfo_height()
        hover = self._index(e.y) if inside else -1
        if hover != self.hover:
            self.hover = hover
            self.redraw()

    def _click(self, e: tk.Event) -> None:
        i = self._index(e.y)
        if i == 0:
            self.on_select(None)
        elif i - 1 < len(self.rows):
            self.on_select(self.rows[i - 1]["run"])

    def redraw(self) -> None:
        self.delete("all")
        w, row_h = self.winfo_width(), px(self.ROW)
        entries = [None] + self.rows
        for i, row in enumerate(entries):
            top = i * row_h - self.offset
            if top + row_h < 0 or top > self.winfo_height():
                continue
            name = None if row is None else row["run"]
            if name == self.selected:
                self.create_rectangle(0, top, w, top + row_h, fill=CARD_HOVER, width=0)
                self.create_rectangle(0, top, px(3), top + row_h, fill=GOLD, width=0)
            elif i == self.hover:
                self.create_rectangle(0, top, w, top + row_h, fill=_blend(CARD, CARD_HOVER, 0.5), width=0)
            self.create_line(px(12), top + row_h, w - px(12), top + row_h, fill=BORDER)
            if row is None:
                self.create_text(px(16), top + row_h / 2, text="+   New run", anchor="w",
                                 fill=GOLD if self.selected is None else TEXT, font=(FONT, 10, "bold"))
                continue
            phase = row["phase"]
            r = px(4)
            cy = top + px(17)
            self.create_oval(px(16) - r, cy - r, px(16) + r, cy + r, fill=TRAIN_COLOURS.get(phase, MUTED),
                             outline="")
            self.create_text(px(28), cy, text=row["run"], anchor="w", fill=TEXT, font=(FONT, 10, "bold"))
            total = row.get("hours total") or 0
            sub = f"u{row.get('update', 0)}  ·  {row.get('hours', 0):.2f} / {total:g} h"
            if row.get("eval"):
                sub += f"  ·  {row['eval']['win rate']:.1%}"
            self.create_text(px(28), top + px(35), text=sub, anchor="w", fill=MUTED, font=(FONT, 8))
            if total:
                bar_w = w - px(28) - px(14)
                self.create_rectangle(px(28), top + row_h - px(5), px(28) + bar_w, top + row_h - px(3),
                                      fill=BORDER, width=0)
                done = max(px(2), int(bar_w * min(row.get("hours", 0) / total, 1)))
                self.create_rectangle(px(28), top + row_h - px(5), px(28) + done, top + row_h - px(3),
                                      fill=TRAIN_COLOURS.get(phase, MUTED) if phase in learning.TRAIN_ACTIVE else MUTED,
                                      width=0)


def _stat_cards(parent: tk.Misc, specs, *, size: int = 13) -> tuple[dict, dict]:
    """A row of stat cards: big value over a small caption. Returns their value vars and labels."""
    values, labels = {}, {}
    for col, (key, name) in enumerate(specs):
        parent.columnconfigure(col, weight=1, uniform="card")
        card = tk.Frame(parent, bg=CARD, padx=px(14), pady=px(6))
        card.grid(row=0, column=col, sticky="nsew", padx=(0 if col == 0 else px(8), 0))
        var = tk.StringVar(value="—")
        lbl = tk.Label(card, textvariable=var, bg=CARD, fg=DIM, font=(FONT, size, "bold"), anchor="w")
        lbl.pack(anchor="w", fill="x")
        tk.Label(card, text=name, bg=CARD, fg=MUTED, font=(FONT, 8, "bold")).pack(anchor="w")
        values[key], labels[key] = var, lbl
    return values, labels


def _fill_cards(values: dict, labels: dict, shown: dict) -> None:
    for key, var in values.items():
        var.set(shown.get(key, "—"))
        labels[key].configure(fg=GOLD if key in shown else DIM)


class TrainTab(tk.Frame):
    """Learning runs: the run list, the chosen run's settings, controls and numbers, and
    its charts by update."""

    def __init__(self, master: tk.Misc, panel: "ControlPanel") -> None:
        super().__init__(master, bg=BG)
        self.panel = panel
        self.history = learning.History()
        self.rows: list[dict] = []
        self.row: dict | None = None
        self.selected: str | None = None
        self.pinned = False
        self.phase = "idle"
        self.playing: set[str] = set()
        # The run this window launched, until its first status shows.
        self.proc: subprocess.Popen | None = None
        self.launched_at = 0.0
        self.launched_run: str | None = None
        self.early: dict | None = None
        self._build(load_settings())

    def _build(self, s: dict) -> None:
        tip = self.panel._tip
        self.runs = RunList(self, self.select, width=px(250))
        self.runs.pack(side="left", fill="y", padx=(px(12), 0), pady=(px(10), px(10)))
        right = tk.Frame(self, bg=BG)
        right.pack(side="left", fill="both", expand=True, padx=px(12), pady=(px(10), px(10)))

        head = tk.Frame(right, bg=BG)
        head.pack(fill="x")
        controls = ttk.Frame(head, style="Card.TFrame", padding=(px(12), px(8)))
        controls.pack(side="left", fill="y")
        self.vars = {
            "train_run": tk.StringVar(value=""),
            "train_copy": tk.StringVar(value=s["train_copy"]),
            "train_hours": tk.DoubleVar(value=s["train_hours"]),
            "train_beta": tk.DoubleVar(value=s["train_beta"]),
        }
        fields = [
            ("RUN", ttk.Entry(controls, width=14, textvariable=self.vars["train_run"])),
            ("COPY", ttk.Combobox(controls, values=learning.copies(), state="readonly", width=10,
                                  textvariable=self.vars["train_copy"])),
            ("HOURS", ttk.Spinbox(controls, from_=0.5, to=200, increment=0.5, width=5,
                                  textvariable=self.vars["train_hours"])),
            ("BETA", ttk.Spinbox(controls, from_=0, to=1, increment=0.01, width=5,
                                 textvariable=self.vars["train_beta"])),
        ]
        self.inputs = []
        for col, (label, widget) in enumerate(fields):
            ttk.Label(controls, text=label, style="Field.TLabel").grid(
                row=0, column=col, sticky="w", padx=(0, px(12)), pady=(0, px(3)))
            widget.grid(row=1, column=col, sticky="w", padx=(0, px(12)))
            self.inputs.append(widget)
        self.vars["train_run"].trace_add("write", lambda *_: self.refresh() if self.selected is None else None)

        buttons = tk.Frame(head, bg=BG)
        buttons.pack(side="left", fill="y", padx=(px(12), 0))
        self.start_btn = ttk.Button(buttons, text="▶  Start", style="Start.TButton", width=8, command=self.start)
        self.start_btn.pack(side="left", fill="y")
        self.resume_btn = ttk.Button(buttons, text="↻  Resume", command=self.resume)
        self.resume_btn.pack(side="left", fill="y", padx=(px(8), 0))
        tip(self.resume_btn, "Carry the run on from its last update, with its own settings")
        self.stop_btn = ttk.Button(buttons, text="■  Stop", style="Stop.TButton", command=self.stop)
        self.stop_btn.pack(side="left", fill="y", padx=(px(8), 0))
        tip(self.stop_btn, "Stop after the current update; Resume carries on")

        state = tk.Frame(right, bg=BG)
        state.pack(fill="x", before=head, pady=(0, px(8)))
        line = tk.Frame(state, bg=BG)
        line.pack(anchor="w")
        self.dot = tk.Label(line, text="●", bg=BG, fg=DIM, font=(FONT, 12))
        self.dot.pack(side="left")
        self.state_text = tk.StringVar()
        tk.Label(line, textvariable=self.state_text, bg=BG, fg=TEXT,
                 font=(FONT, 11, "bold")).pack(side="left", padx=(px(6), 0))
        self.note = tk.StringVar()
        note = tk.Label(state, textvariable=self.note, bg=BG, fg=MUTED, font=(FONT, 9), anchor="w",
                        justify="left")
        note.pack(anchor="w", fill="x")
        note.bind("<Configure>", lambda e: note.configure(wraplength=e.width))
        tip(state, lambda: learning.console_tail(self.selected) or "No output yet")

        cards = tk.Frame(right, bg=BG)
        cards.pack(fill="x", pady=(px(10), 0))
        self.card_vals, self.card_labels = _stat_cards(cards, (
            ("update", "UPDATE"), ("hours", "HOURS"), ("games", "GAMES"), ("points", "POINTS"),
            ("agreement", "AGREEMENT"), ("drift", "DRIFT"), ("eval", "EVAL WINS"), ("ante", "EVAL ANTE")))

        charts = tk.Frame(right, bg=BG)
        charts.pack(fill="both", expand=True, pady=(px(10), 0))
        tooltip = self.panel.tooltip
        self.charts = {
            "points": LineChart(charts, tooltip, "POINTS PER GAME", _num(2)),
            "agreement": LineChart(charts, tooltip, "AGREEMENT WITH THE COPY", _pct),
            "drift": LineChart(charts, tooltip, "DRIFT FROM THE COPY", _num(4)),
            "eval": LineChart(charts, tooltip, "EVAL WIN RATE", _pct),
            "ante": LineChart(charts, tooltip, "EVAL MEAN FINAL ANTE", _num(2)),
        }
        for col in range(6):
            charts.columnconfigure(col, weight=1, uniform="chart")
        charts.rowconfigure(0, weight=1)
        charts.rowconfigure(1, weight=1)
        for i, key in enumerate(("points", "agreement", "drift")):
            self.charts[key].grid(row=0, column=2 * i, columnspan=2, sticky="nsew",
                                  padx=(0 if i == 0 else px(8), 0), pady=(0, px(8)))
        for i, key in enumerate(("eval", "ante")):
            self.charts[key].grid(row=1, column=3 * i, columnspan=3, sticky="nsew",
                                  padx=(0 if i == 0 else px(8), 0))

    # ── Actions ──

    def select(self, name: str | None) -> None:
        self.selected, self.pinned = name, True
        if name is None:
            s = load_settings()
            self.vars["train_run"].set("")
            self.vars["train_copy"].set(s["train_copy"])
            self.vars["train_hours"].set(s["train_hours"])
            self.vars["train_beta"].set(s["train_beta"])
        self.refresh()

    def _settings(self) -> dict:
        try:
            settings = {
                "train_run": self.vars["train_run"].get().strip(),
                "train_copy": self.vars["train_copy"].get(),
                "train_hours": float(self.vars["train_hours"].get()),
                "train_beta": float(self.vars["train_beta"].get()),
            }
        except (tk.TclError, ValueError):
            raise ValueError("Hours and beta must be numbers.")
        name = settings["train_run"]
        if not learning.valid_run_name(name):
            raise ValueError("Name the run with letters, digits, - _ and . only.")
        if learning.run_taken(name) or name in self.playing:
            raise ValueError(f"{name} already exists. Pick it in the list to resume it, or use another name.")
        if settings["train_copy"] not in learning.copies():
            raise ValueError("Pick the copy to start from.")
        if settings["train_hours"] <= 0:
            raise ValueError("Hours must be above 0.")
        if settings["train_beta"] < 0:
            raise ValueError("Beta can't be negative.")
        return settings

    def _others_ok(self) -> bool:
        """Ask before adding a run to a batch or to another run already playing."""
        if batch_active() and not self.panel._overlap_ok("A bot batch"):
            return False
        return not self.playing or self.panel._overlap_ok(f"Learning run {sorted(self.playing)[0]}")

    def start(self) -> None:
        if self.selected is not None:
            return
        try:
            settings = self._settings()
        except ValueError as e:
            messagebox.showerror("JackPotts", str(e), parent=self.panel.root)
            return
        if not self._others_ok():
            return
        save_settings(settings)
        run = settings["train_run"]
        self._launched(learning.launch_training(learning.train_command(settings), run), run)

    def resume(self) -> None:
        name = self.selected
        if name is None or name in self.playing or not learning.resumable(name) or self.phase == "outside":
            return
        if not self._others_ok():
            return
        state = learning.run_state(name)
        self._launched(learning.launch_training(learning.resume_command(state["args"]), name, append=True),
                       name)

    def _launched(self, proc: subprocess.Popen, run: str) -> None:
        self.proc, self.launched_at, self.launched_run, self.early = proc, time.time(), run, None
        self.selected, self.pinned = run, True
        self.refresh()

    def stop(self) -> None:
        if self.phase != "running":
            return
        if not messagebox.askyesno("JackPotts", f"Stop {self.selected} after this update? "
                                   "It saves, and Resume carries it on.", parent=self.panel.root):
            return
        learning.train_file(self.selected, ".stop").touch()
        self.refresh()

    # ── Refresh ──

    def refresh(self) -> None:
        statuses = learning.read_trainings()
        launched = statuses.get(self.launched_run) if self.launched_run else None
        fresh = launched is not None and launched.get("updated", 0) >= self.launched_at
        if self.proc is not None:
            if fresh:
                self.proc = None
            elif self.proc.poll() is not None:
                # Gone before its first status: torch missing, a bad flag and the like.
                self.early = {"run": self.launched_run, "phase": "failed", "updated": time.time(),
                              "error": learning.console_tail(self.launched_run)
                              or f"Exited with code {self.proc.returncode}."}
                self.proc = None
        if self.early is not None:
            if fresh:
                self.early = None
            else:
                statuses[self.launched_run] = {**(launched or {}), **self.early}
        if self.proc is not None:
            statuses[self.launched_run] = {**(launched or {}), "run": self.launched_run, "phase": "starting",
                                           "updated": time.time()}
        self.rows = learning.list_runs(statuses)
        by_name = {r["run"]: r for r in self.rows}
        playing = {r["run"] for r in self.rows if r["phase"] in learning.TRAIN_ACTIVE}
        self._notify(playing, by_name)
        self.playing = playing

        if not self.pinned or (self.selected is not None and self.selected not in by_name):
            current = by_name.get(self.selected)
            if current is None or current["phase"] not in learning.TRAIN_ACTIVE:
                self.selected = next(iter(playing), None) if playing else (
                    self.selected if current else (self.rows[0]["run"] if self.rows else None))
        self.row = by_name.get(self.selected) if self.selected is not None else None
        self.phase = self.row["phase"] if self.row else "idle"
        self.runs.update_rows(self.rows, self.selected)
        self._fill_head()
        self._fill_cards()
        if self.winfo_ismapped():
            self._fill_charts()

    def _notify(self, playing: set[str], by_name: dict) -> None:
        """Once per run that stops playing: finished, stopped or failed."""
        for name in self.playing - playing:
            row = by_name.get(name)
            if row is None:
                continue
            body = row.get("error") if row["phase"] == "failed" else learning.training_summary(row)
            self.panel.tray.notify(f"Learning run {name} {row['phase']}", body or row["phase"])

    def _fill_head(self) -> None:
        row, phase = self.row, self.phase
        new = self.selected is None
        if not new:
            for key, value in (("train_run", row["run"]), ("train_copy", row.get("copy") or ""),
                               ("train_hours", row.get("hours total") or 0), ("train_beta", row.get("beta") or 0)):
                if str(self.vars[key].get()) != str(value):
                    self.vars[key].set(value)
        for w in self.inputs:
            w.state(["!disabled"] if new else ["disabled"])
        name = self.vars["train_run"].get().strip()
        can_start = (new and learning.valid_run_name(name) and not learning.run_taken(name)
                     and name not in self.playing)
        can_resume = (not new and phase in ("stopped", "failed") and self.selected not in self.playing
                      and learning.resumable(self.selected))
        for btn, on in ((self.start_btn, can_start), (self.resume_btn, can_resume),
                        (self.stop_btn, phase == "running")):
            btn.state(["!disabled"] if on else ["disabled"])
        self.dot.configure(fg=TRAIN_COLOURS.get(phase, MUTED) if not new else GOLD)
        self.state_text.set("New run" if new else f"{phase.capitalize()}  ·  {row['run']}")
        self.note.set(self._note(name, can_resume))

    def _note(self, name: str, can_resume: bool) -> str:
        row, phase = self.row or {}, self.phase
        if self.selected is None:
            if name and learning.run_taken(name):
                return f"{name} already exists: pick it in the list to see or resume it."
            return "Name the run, pick the copy, then Start."
        if phase == "starting" or row.get("doing") == "starting":
            return "Loading the model…"
        if phase == "running":
            doing = "Evaluating" if row.get("doing") == "evaluating" else "Playing and learning"
            return f"{doing}  ·  from {row.get('copy') or '?'}"
        if phase == "stopping":
            return "Stopping after this update; it saves first."
        if phase == "failed":
            return (row.get("error") or "Failed.") + ("  Resume carries it on." if can_resume else "")
        if phase == "outside":
            return "Its log changed in the last 10 minutes: it may still be playing from a terminal."
        if phase == "stopped":
            return (f"Stopped at update {row.get('update', 0)}. Resume carries it on." if can_resume
                    else "Stopped before its first update.")
        return f"Played its {row.get('hours total') or 0:g} h over {row.get('update', 0)} updates."

    def _fill_cards(self) -> None:
        row, shown = self.row or {}, {}
        if self.row is not None and self.phase != "starting":
            shown = {"update": f"{row.get('update', 0):,}", "games": f"{row.get('games', 0):,}"}
            if row.get("hours total"):
                shown["hours"] = f"{row.get('hours', 0):.2f} / {row['hours total']:g}"
            for key, field, fmt in (("points", "points per game", _num(2)), ("agreement", "agreement", _pct),
                                    ("drift", "drift", _num(4))):
                if row.get(field) is not None:
                    shown[key] = fmt(row[field])
            ev = row.get("eval")
            if ev:
                shown["eval"], shown["ante"] = _pct(ev["win rate"]), f"{ev['mean final ante']:.2f}"
        _fill_cards(self.card_vals, self.card_labels, shown)

    def _fill_charts(self) -> None:
        data = self.history.of(self.selected) if self.selected else {"update": [], "points": [],
                                                                     "agreement": [], "drift": [], "evals": []}
        for key in ("points", "agreement", "drift"):
            self.charts[key].set(data["update"], data[key])
        evals = data["evals"]
        self.charts["eval"].set([e[0] for e in evals], [e[1] for e in evals], [e[2] for e in evals])
        self.charts["ante"].set([e[0] for e in evals], [e[3] for e in evals])

    def mini_line(self) -> tuple[str, str]:
        """The compact view's line: (colour, text)."""
        row = next((r for r in self.rows if r["phase"] in learning.TRAIN_ACTIVE), None)
        if row is None:
            return DIM, "No learning run playing"
        text = f"{row['run']}   u{row.get('update', 0)}   {row.get('hours', 0):.2f} / {row.get('hours total') or 0:g} h"
        if row.get("eval"):
            text += f"   eval {row['eval']['win rate']:.1%}"
        return TRAIN_COLOURS.get(row["phase"], MUTED), text


STREAM_COLOURS = {"idle": DIM, "starting": BLUE, "running": GREEN, "stopping": YELLOW,
                  "stopped": MUTED, "failed": RED}
OUTCOME_COLOURS = {"won": GREEN, "lost": MUTED, "divergence": RED, "error": RED, "stuck": YELLOW,
                   "not ported": YELLOW, "stopped": DIM}


class RecentStream(tk.Canvas):
    """The stream's latest games: outcome, where it ended, the seed and the save that played."""

    ROW = 30

    def __init__(self, master: tk.Misc, tooltip: Tooltip, **kw) -> None:
        super().__init__(master, bg=CARD, highlightthickness=0, bd=0, **kw)
        self.tooltip = tooltip
        self.games: list[dict] = []
        self.bind("<Configure>", lambda _e: self.redraw())
        self.bind("<Motion>", self._motion)
        self.bind("<Leave>", lambda _e: self.tooltip.hide())

    def update_games(self, games: list[dict]) -> None:
        if games != self.games:
            self.games = games
            self.redraw()

    def redraw(self) -> None:
        self.delete("all")
        w = self.winfo_width()
        self.create_text(px(14), px(16), text="RECENT GAMES", anchor="w", fill=MUTED, font=(FONT, 8, "bold"))
        if not self.games:
            self.create_text(w / 2, px(60), text="None yet", fill=DIM, font=(FONT, 10))
            return
        cols = (px(30), px(170), px(300), px(430))
        for i, g in enumerate(self.games):
            top = px(32) + i * px(self.ROW)
            mid = top + px(self.ROW) / 2
            r = px(4)
            self.create_oval(px(16) - r, mid - r, px(16) + r, mid + r,
                             fill=OUTCOME_COLOURS.get(g["outcome"], MUTED), outline="")
            where = "won" if g["outcome"] == "won" else f"{g['outcome']}, ante {g.get('ante', '?')}"
            self.create_text(cols[0], mid, text=where, anchor="w", fill=TEXT, font=(FONT, 10))
            self.create_text(cols[1], mid, text=g.get("seed", ""), anchor="w", fill=MUTED, font=(FONT, 9))
            self.create_text(cols[2], mid, text=g.get("checkpoint", ""), anchor="w", fill=MUTED, font=(FONT, 9))
            self.create_text(cols[3], mid, text=f"{g.get('steps', 0)} moves", anchor="w", fill=DIM,
                             font=(FONT, 9))
            self.create_line(px(12), top + px(self.ROW), w - px(12), top + px(self.ROW), fill=BORDER)

    def _motion(self, e: tk.Event) -> None:
        i = int((e.y - px(32)) // px(self.ROW))
        if 0 <= i < len(self.games) and e.y >= px(32):
            g = self.games[i]
            text = g.get("error") or f"{g['outcome']} in {g.get('seconds', 0):.0f}s"
            if g.get("bundle"):
                text += f"\n{g['bundle']}"
            self.tooltip.show(text, e.x_root, e.y_root)
        else:
            self.tooltip.hide()


class StreamTab(tk.Frame):
    """The stream: a model playing the visible game. Pick the model and the run's save,
    start it, and watch the game, the totals and the latest games."""

    def __init__(self, master: tk.Misc, panel: "ControlPanel") -> None:
        super().__init__(master, bg=BG)
        self.panel = panel
        self.status: dict | None = None
        self.now: dict | None = None
        self.phase = "idle"
        self.proc: subprocess.Popen | None = None
        self.launched_at = 0.0
        self.early: dict | None = None
        self.was_active = False
        self._build(load_settings())

    def _build(self, s: dict) -> None:
        tip = self.panel._tip
        head = tk.Frame(self, bg=BG)
        head.pack(fill="x", padx=px(12))
        controls = ttk.Frame(head, style="Card.TFrame", padding=(px(12), px(8)))
        controls.pack(side="left", fill="y")
        sources = learning.model_sources()
        source = s["stream_source"] if s["stream_source"] in sources else (sources[0] if sources else "")
        self.vars = {
            "stream_source": tk.StringVar(value=source),
            "stream_checkpoint": tk.StringVar(value=s["stream_checkpoint"]),
            "stream_deck": tk.StringVar(value=s["stream_deck"]),
            "stream_stake": tk.StringVar(value=s["stream_stake"]),
            "stream_delay": tk.DoubleVar(value=s["stream_delay"]),
        }
        self.source_input = ttk.Combobox(controls, values=sources, state="readonly", width=18,
                                         textvariable=self.vars["stream_source"])
        self.save_input = ttk.Combobox(controls, state="readonly", width=10,
                                       textvariable=self.vars["stream_checkpoint"])
        fields = [
            ("MODEL", self.source_input),
            ("SAVE", self.save_input),
            ("DECK", ttk.Combobox(controls, values=DECKS, state="readonly", width=10,
                                  textvariable=self.vars["stream_deck"])),
            ("STAKE", ttk.Combobox(controls, values=STAKES, state="readonly", width=8,
                                   textvariable=self.vars["stream_stake"])),
            ("DELAY", ttk.Spinbox(controls, from_=0, to=10, increment=0.5, width=4,
                                  textvariable=self.vars["stream_delay"])),
        ]
        self.inputs = []
        for col, (label, widget) in enumerate(fields):
            ttk.Label(controls, text=label, style="Field.TLabel").grid(
                row=0, column=col, sticky="w", padx=(0, px(12)), pady=(0, px(3)))
            widget.grid(row=1, column=col, sticky="w", padx=(0, px(12)))
            self.inputs.append(widget)
        tip(self.save_input, "latest follows the run: the stream reads it again before each game")
        self.vars["stream_source"].trace_add("write", lambda *_: self._sync_saves())
        self._sync_saves()

        buttons = tk.Frame(head, bg=BG)
        buttons.pack(side="left", fill="y", padx=(px(12), 0))
        self.start_btn = ttk.Button(buttons, text="▶  Start", style="Start.TButton", width=8, command=self.start)
        self.start_btn.pack(side="left", fill="y")
        self.stop_btn = ttk.Button(buttons, text="■  Stop", style="Stop.TButton", command=self.stop)
        self.stop_btn.pack(side="left", fill="y", padx=(px(8), 0))
        tip(self.stop_btn, "Stop after the current move and close the game")

        state = tk.Frame(self, bg=BG)
        state.pack(fill="x", before=head, padx=px(12), pady=(px(10), px(8)))
        line = tk.Frame(state, bg=BG)
        line.pack(anchor="w")
        self.dot = tk.Label(line, text="●", bg=BG, fg=DIM, font=(FONT, 12))
        self.dot.pack(side="left")
        self.state_text = tk.StringVar()
        tk.Label(line, textvariable=self.state_text, bg=BG, fg=TEXT,
                 font=(FONT, 11, "bold")).pack(side="left", padx=(px(6), 0))
        self.note = tk.StringVar()
        note = tk.Label(state, textvariable=self.note, bg=BG, fg=MUTED, font=(FONT, 9), anchor="w",
                        justify="left")
        note.pack(anchor="w", fill="x")
        note.bind("<Configure>", lambda e: note.configure(wraplength=e.width))

        game = tk.Frame(self, bg=CARD, padx=px(16), pady=px(12))
        game.pack(fill="x", padx=px(12), pady=(px(10), 0))
        self.game_title = tk.StringVar(value="—")
        self.game_line = tk.StringVar()
        self.game_sub = tk.StringVar()
        tk.Label(game, textvariable=self.game_title, bg=CARD, fg=GOLD, font=(FONT, 18, "bold"),
                 anchor="w").pack(anchor="w", fill="x")
        tk.Label(game, textvariable=self.game_line, bg=CARD, fg=TEXT, font=(FONT, 11),
                 anchor="w").pack(anchor="w", fill="x", pady=(px(2), 0))
        tk.Label(game, textvariable=self.game_sub, bg=CARD, fg=MUTED, font=(FONT, 9),
                 anchor="w").pack(anchor="w", fill="x", pady=(px(4), 0))
        self.score_bar = tk.Canvas(game, bg=CARD, height=px(4), highlightthickness=0, bd=0)
        self.score_bar.pack(fill="x", pady=(px(8), 0))

        totals = tk.Frame(self, bg=BG)
        totals.pack(fill="x", padx=px(12), pady=(px(10), 0))
        self.card_vals, self.card_labels = _stat_cards(totals, (
            ("games", "GAMES"), ("wins", "WINS"), ("rate", "WIN RATE"), ("divergences", "DIVERGENCES"),
            ("save", "PLAYING")), size=15)

        self.recent = RecentStream(self, self.panel.tooltip)
        self.recent.pack(fill="both", expand=True, padx=px(12), pady=(px(10), px(10)))

    def _sync_saves(self) -> None:
        source = self.vars["stream_source"].get()
        saves = learning.checkpoints(source) if source else []
        self.save_input.configure(values=saves)
        if self.vars["stream_checkpoint"].get() not in saves:
            self.vars["stream_checkpoint"].set(saves[0] if saves else "")

    # ── Actions ──

    def _settings(self) -> dict:
        try:
            settings = {k: v.get() for k, v in self.vars.items()}
            settings["stream_delay"] = float(settings["stream_delay"])
        except (tk.TclError, ValueError):
            raise ValueError("Delay must be a number of seconds.")
        if not settings["stream_source"] or not settings["stream_checkpoint"]:
            raise ValueError("Pick the model to play.")
        path, _ = learning.model_choice(settings["stream_source"], settings["stream_checkpoint"])
        if not path.exists():
            raise ValueError(f"{path} is missing.")
        if settings["stream_delay"] < 0:
            raise ValueError("Delay can't be negative.")
        return settings

    def start(self) -> None:
        if self.phase in ("starting", "running", "stopping"):
            return
        try:
            settings = self._settings()
        except ValueError as e:
            messagebox.showerror("JackPotts", str(e), parent=self.panel.root)
            return
        if batch_active() and not self.panel._overlap_ok("A bot batch"):
            return
        save_settings(settings)
        self.proc = learning.launch_stream(learning.stream_command(settings))
        self.launched_at, self.early = time.time(), None
        self.refresh()

    def stop(self) -> None:
        if self.phase not in ("starting", "running"):
            return
        learning.stream_file("stop").touch()
        self.refresh()

    # ── Refresh ──

    def refresh(self) -> None:
        status, now = learning.read_stream()
        fresh = status is not None and status.get("updated", 0) >= self.launched_at
        if self.proc is not None:
            if fresh:
                self.proc = None
            elif self.proc.poll() is not None:
                self.early = {"phase": "failed", "error": _last_line(learning.stream_file("launcher.log"))
                              or f"Exited with code {self.proc.returncode}."}
                self.proc = None
        if self.early is not None:
            if fresh:
                self.early = None
            else:
                status = self.early
        if self.proc is not None:
            phase = "starting"
        elif status is None:
            phase = "idle"
        else:
            phase = status["phase"]
            if phase == "running" and learning.stream_file("stop").exists():
                phase = "stopping"
        self.status, self.now, self.phase = status, now if phase != "idle" else None, phase
        self._notify()

        active = phase in ("starting", "running", "stopping")
        if active and status and status.get("model"):
            source, save = learning.choice_of(status["model"])
            for key, value in (("stream_source", source), ("stream_checkpoint", save),
                               ("stream_deck", status.get("deck")), ("stream_stake", status.get("stake")),
                               ("stream_delay", status.get("delay"))):
                if value is not None and str(self.vars[key].get()) != str(value):
                    self.vars[key].set(value)
        for w in self.inputs:
            w.state(["disabled"] if active else ["!disabled"])
        if not active:
            self.source_input.state(["readonly"])
            sources = learning.model_sources()
            if list(self.source_input.cget("values")) != sources:
                self.source_input.configure(values=sources)
        self.start_btn.state(["disabled"] if active else ["!disabled"])
        self.stop_btn.state(["!disabled"] if phase in ("starting", "running") else ["disabled"])
        self.dot.configure(fg=STREAM_COLOURS.get(phase, MUTED))
        self.state_text.set("Stream off" if phase == "idle" else f"Stream {phase}")
        self.note.set(self._note(status or {}, now or {}))
        self._fill_game(now if active or phase in ("stopped", "failed") else None)

    def _notify(self) -> None:
        """Once, when a stream ends on its own with an error. Divergences and stops the
        panel asked for are on the tab, not in notifications."""
        active = self.phase in ("starting", "running", "stopping")
        if self.was_active and self.phase == "failed":
            self.panel.tray.notify("Stream failed", (self.status or {}).get("error") or "")
        self.was_active = active

    def _note(self, status: dict, now: dict) -> str:
        phase = self.phase
        if phase == "idle":
            return "Pick the model and its save, then Start. The game opens in its own window."
        if phase == "starting":
            return "Opening the game…"
        if phase == "running":
            return (f"Playing {now.get('checkpoint') or '…'}  ·  {status.get('deck', '')} deck, "
                    f"{status.get('stake', '')} stake  ·  {status.get('delay', 0):g}s a move")
        if phase == "stopping":
            return "Stopping after this move, then closing the game."
        if phase == "failed":
            return status.get("error") or "Failed."
        return status.get("note", "Stopped.").capitalize()

    def _fill_game(self, now: dict | None) -> None:
        self.score_bar.delete("all")
        game = (now or {}).get("game") or {}
        if not game:
            self.game_title.set("No game")
            self.game_line.set("")
            self.game_sub.set("")
            _fill_cards(self.card_vals, self.card_labels, {})
            self.recent.update_games([])
            return
        # The game shown is one past those counted unless it is the last one counted.
        last = (now.get("recent") or [{}])[0]
        counted = last.get("seed") == game.get("seed") and last.get("outcome") != "stopped"
        number = now.get("games", 0) + (0 if counted else 1)
        self.game_title.set(f"Game {number}   ·   Ante {game.get('ante', '?')}   ·   Round {game.get('round', '?')}")
        blind = game.get("blind") or 0
        line = f"${game.get('money', 0)}"
        if blind:
            line += f"   ·   {game.get('chips', 0):,} / {blind:,} chips"
        line += f"   ·   {game.get('hands', 0)} hands, {game.get('discards', 0)} discards"
        self.game_line.set(line)
        self.game_sub.set(f"seed {game.get('seed', '')}   ·   last: {game.get('last') or '—'}")
        ControlPanel._bar(self.score_bar, game.get("chips", 0) / blind if blind else 0)
        games, wins = now.get("games", 0), now.get("wins", 0)
        shown = {"games": f"{games:,}", "wins": f"{wins:,}", "divergences": f"{now.get('divergences', 0):,}",
                 "save": now.get("checkpoint") or "—"}
        if games:
            shown["rate"] = _pct(wins / games)
        _fill_cards(self.card_vals, self.card_labels, shown)
        self.recent.update_games(now.get("recent", []))

    def mini_line(self) -> tuple[str, str]:
        if self.phase == "idle":
            return DIM, "Stream off"
        now = self.now or {}
        game = now.get("game") or {}
        text = f"Stream {self.phase}"
        if game:
            text += f"   ante {game.get('ante', '?')}   {now.get('wins', 0)} / {now.get('games', 0)} won"
        return STREAM_COLOURS.get(self.phase, MUTED), text


def _last_line(path: Path) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return next((ln.strip() for ln in reversed(lines) if ln.strip()), "")


class ControlPanel:
    def __init__(self, root: tk.Tk) -> None:
        global _scale
        _scale = root.winfo_fpixels("1i") / 96
        self.root = root
        self.stop_requested = False
        self.pause_requested = False
        self.launched_at = 0.0
        # What a just-launched supervisor is doing ("starting" or "stopping") until its status shows.
        self.pending = "starting"
        self.running = False
        self.paused: dict | None = None
        self.compact = False
        self.full_geometry = f"{px(1180)}x{px(740)}"
        self.watched_session: int | None = None
        self.tab = "train"
        root.title("JackPotts")
        root.configure(bg=BG)
        root.minsize(px(FULL_MIN[0]), px(FULL_MIN[1]))
        root.geometry(self.full_geometry)
        self._style()
        self.tooltip = Tooltip(root)
        self._build()
        self.tray = TrayIcon(on_open=self.show, on_quit=self.quit)
        root.protocol("WM_DELETE_WINDOW", root.withdraw)
        self.refresh()

    def show(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def quit(self) -> None:
        self.tray.close()
        self.root.destroy()

    # ── Layout ──

    def _style(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", background=BG, foreground=TEXT, fieldbackground=BG,
                        bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER, font=(FONT, 10))
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD)
        style.configure("Field.TLabel", background=CARD, foreground=MUTED, font=(FONT, 8, "bold"))
        style.configure("Hint.TLabel", foreground=DIM, font=(FONT, 9))
        style.configure("Card.TCheckbutton", background=CARD, foreground=TEXT,
                        indicatorbackground=BG, indicatorforeground=GOLD, focuscolor=CARD,
                        indicatorsize=px(16), padding=(0, px(3)))
        style.map("Card.TCheckbutton", background=[("active", CARD)],
                  indicatorbackground=[("disabled", CARD)], indicatorforeground=[("disabled", DIM)])
        style.configure("TSpinbox", arrowcolor=TEXT, foreground=TEXT, fieldbackground=BG,
                        background=CARD, arrowsize=px(11))
        style.map("TSpinbox", foreground=[("disabled", DIM)], fieldbackground=[("disabled", CARD)],
                  arrowcolor=[("disabled", DIM)])
        style.configure("TEntry", foreground=TEXT, fieldbackground=BG, insertcolor=TEXT)
        style.map("TEntry", foreground=[("disabled", DIM)], fieldbackground=[("disabled", CARD)])
        style.configure("TCombobox", arrowcolor=TEXT, foreground=TEXT, background=CARD)
        style.map("TCombobox", fieldbackground=[("disabled", CARD), ("readonly", BG)],
                  foreground=[("disabled", DIM), ("readonly", TEXT)], arrowcolor=[("disabled", DIM)])
        style.configure("TButton", background=CARD, foreground=TEXT, padding=(px(12), px(6)),
                        borderwidth=1, focusthickness=0, focuscolor=CARD)
        style.map("TButton", background=[("disabled", BG), ("active", CARD_HOVER)],
                  foreground=[("disabled", DIM)], bordercolor=[("disabled", BORDER)])
        style.configure("Start.TButton", foreground=BG, background=GREEN, bordercolor=GREEN,
                        font=(FONT, 10, "bold"))
        style.map("Start.TButton", background=[("disabled", CARD), ("active", "#5cbf60")],
                  foreground=[("disabled", DIM)], bordercolor=[("disabled", BORDER)])
        style.configure("Stop.TButton", foreground=RED, bordercolor=RED, font=(FONT, 10, "bold"))
        style.map("Stop.TButton", bordercolor=[("disabled", BORDER)])
        style.configure("Icon.TButton", padding=(px(8), px(6)), font=(FONT, 11))
        style.configure("IconStart.TButton", padding=(px(8), px(6)), font=(FONT, 11), foreground=GREEN)
        style.configure("IconStop.TButton", padding=(px(8), px(6)), font=(FONT, 11), foreground=RED)
        self.root.option_add("*TCombobox*Listbox.background", CARD)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", BORDER)

    def _build(self) -> None:
        self.strip = tk.Frame(self.root, bg=BORDER, height=px(3))
        self.strip.pack(fill="x", side="top")
        self.full = ttk.Frame(self.root)
        self.full.pack(fill="both", expand=True)
        header = tk.Frame(self.full, bg=BG)
        header.pack(fill="x", padx=px(12), pady=(px(8), 0))
        self._build_tabs(header)
        links = tk.Frame(header, bg=BG)
        links.pack(side="right", fill="y")
        for text, tip, cmd in (("⤡", "Compact", self.toggle_compact),
                               ("☰", "Logs folder", lambda: os.startfile(LOG_DIR)),
                               ("↗", "Dashboard", lambda: webbrowser.open(DASHBOARD_URL))):
            b = ttk.Button(links, text=text, style="Icon.TButton", width=2, command=cmd)
            b.pack(side="right", fill="y", padx=(px(6), 0))
            self._tip(b, tip)
        tk.Frame(self.full, bg=BORDER, height=1).pack(fill="x", padx=px(12))
        pages = tk.Frame(self.full, bg=BG)
        pages.pack(fill="both", expand=True)
        self.pages = {"batches": ttk.Frame(pages)}
        self._build_full(self.pages["batches"])
        self.train = TrainTab(pages, self)
        self.stream = StreamTab(pages, self)
        self.pages["train"], self.pages["stream"] = self.train, self.stream
        self.show_tab("train")
        self.mini = ttk.Frame(self.root)
        self._build_mini(self.mini)

    def _build_tabs(self, header: tk.Frame) -> None:
        """Train and Stream up front; Batches small at the end, there when it's wanted."""
        self.tab_widgets = {}
        for key, text, size in (("train", "Train", 12), ("stream", "Stream", 12), ("batches", "Batches", 9)):
            tab = tk.Frame(header, bg=BG, cursor="hand2")
            tab.pack(side="left", padx=(0, px(22)))
            row = tk.Frame(tab, bg=BG)
            row.pack(pady=(px(4), px(5)))
            dot = tk.Label(row, text="●", bg=BG, fg=DIM, font=(FONT, 9))
            dot.pack(side="left", padx=(0, px(6)))
            label = tk.Label(row, text=text, bg=BG, fg=MUTED, font=(FONT, size, "bold"))
            label.pack(side="left")
            line = tk.Frame(tab, bg=BG, height=px(3))
            line.pack(fill="x")
            for w in (tab, row, dot, label):
                w.bind("<Button-1>", lambda _e, k=key: self.show_tab(k))
            self.tab_widgets[key] = (label, dot, line)

    def show_tab(self, key: str) -> None:
        self.tab = key
        for name, page in self.pages.items():
            if name == key:
                page.pack(fill="both", expand=True)
            else:
                page.pack_forget()
        for name, (label, _dot, line) in self.tab_widgets.items():
            label.configure(fg=TEXT if name == key else MUTED)
            line.configure(bg=GOLD if name == key else BG)
        self.tooltip.hide()
        if hasattr(self, "tray"):
            self.refresh(reschedule=False)

    def _build_full(self, parent: ttk.Frame) -> None:
        s = load_settings()
        top = ttk.Frame(parent)
        top.pack(fill="x", padx=px(12), pady=(px(10), 0))

        controls = ttk.Frame(top, style="Card.TFrame", padding=(px(12), px(8)))
        controls.pack(side="left", fill="y")
        self.vars = {
            "instances": tk.IntVar(value=s["instances"]),
            "games": tk.IntVar(value=s["games"]),
            "deck": tk.StringVar(value=s["deck"]),
            "stake": tk.StringVar(value=s["stake"]),
            "explore_instances": tk.IntVar(value=s["explore_instances"]),
            "explore_rate": tk.DoubleVar(value=s["explore_rate"]),
            "sim": tk.BooleanVar(value=bool(s["sim"])),
            "kernel": tk.BooleanVar(value=bool(s.get("kernel"))),
        }
        self.explore_rate_input = ttk.Spinbox(controls, from_=0.05, to=1.0, increment=0.05, width=5,
                                              format="%.2f", textvariable=self.vars["explore_rate"])
        fields = [
            ("INSTANCES", ttk.Spinbox(controls, from_=1, to=MAX_INSTANCES, width=4,
                                      textvariable=self.vars["instances"])),
            ("GAMES EACH", ttk.Spinbox(controls, from_=1, to=100000, increment=100, width=7,
                                       textvariable=self.vars["games"])),
            ("DECK", ttk.Combobox(controls, values=DECKS, state="readonly", width=11,
                                  textvariable=self.vars["deck"])),
            ("STAKE", ttk.Combobox(controls, values=STAKES, state="readonly", width=8,
                                   textvariable=self.vars["stake"])),
            ("EXPLORING", ttk.Spinbox(controls, from_=0, to=MAX_INSTANCES, width=4,
                                      textvariable=self.vars["explore_instances"])),
            ("RATE", self.explore_rate_input),
            ("SIMULATOR", ttk.Checkbutton(controls, style="Card.TCheckbutton",
                                          variable=self.vars["sim"])),
            ("KERNEL", ttk.Checkbutton(controls, style="Card.TCheckbutton",
                                       variable=self.vars["kernel"])),
        ]
        self.inputs = []
        for col, (label, widget) in enumerate(fields):
            ttk.Label(controls, text=label, style="Field.TLabel").grid(
                row=0, column=col, sticky="w", padx=(0, px(12)), pady=(0, px(3)))
            widget.grid(row=1, column=col, sticky="w", padx=(0, px(12)))
            self.inputs.append(widget)
        self.vars["explore_instances"].trace_add("write", lambda *_: self._sync_inputs())

        buttons = ttk.Frame(top)
        buttons.pack(side="left", fill="y", padx=(px(12), 0))
        self.start_btn = ttk.Button(buttons, text="▶  Start", style="Start.TButton", width=9, command=self.start)
        self.start_btn.pack(side="left", fill="y")
        self.pause_btn = ttk.Button(buttons, text="❚❚  Pause", command=self.pause)
        self.pause_btn.pack(side="left", fill="y", padx=(px(8), 0))
        self._tip(self.pause_btn, "Close the games and keep the counts; Resume carries on")
        self.stop_btn = ttk.Button(buttons, text="■  Stop", style="Stop.TButton", command=self.stop)
        self.stop_btn.pack(side="left", fill="y", padx=(px(8), 0))

        stats = ttk.Frame(parent)
        stats.pack(fill="x", padx=px(12), pady=(px(10), 0))
        self.stat_vals: dict[str, tk.StringVar] = {}
        self.stat_labels: dict[str, tk.Label] = {}
        for col, (key, name) in enumerate((("batch", "BATCH"), ("games", "GAMES"), ("wins", "WINS"),
                                           ("rate", "GAMES / HOUR"), ("eta", "ETA"))):
            stats.columnconfigure(col, weight=2 if key == "games" else 1, uniform="stat")
            card = tk.Frame(stats, bg=CARD, padx=px(14), pady=px(8))
            card.grid(row=0, column=col, sticky="nsew", padx=(0 if col == 0 else px(8), 0))
            row = tk.Frame(card, bg=CARD)
            row.pack(anchor="w", fill="x")
            var = tk.StringVar(value="—")
            self.stat_vals[key] = var
            lbl = tk.Label(row, textvariable=var, bg=CARD, fg=GOLD, font=(FONT, 15, "bold"))
            lbl.pack(side="left")
            self.stat_labels[key] = lbl
            if key == "wins":
                self.win_pct = tk.StringVar()
                tk.Label(row, textvariable=self.win_pct, bg=CARD, fg=GREEN,
                         font=(FONT, 11, "bold")).pack(side="left", padx=(px(8), 0), pady=(px(4), 0))
            if key == "games":
                self.games_bar = tk.Canvas(card, bg=CARD, height=px(4), highlightthickness=0, bd=0)
                self.games_bar.pack(fill="x", pady=(px(4), px(2)))
            tk.Label(card, text=name, bg=CARD, fg=MUTED, font=(FONT, 8, "bold")).pack(anchor="w")

        tools = ttk.Frame(parent)
        tools.pack(fill="x", padx=px(12), pady=(px(10), 0))
        self.add_btn = ttk.Button(tools, text="+", style="Icon.TButton", width=2, command=self.add_instance)
        self.add_btn.pack(side="right")
        self._tip(self.add_btn, "Add an instance")
        self.add_vars = {"deck": tk.StringVar(value=s["deck"]), "stake": tk.StringVar(value=s["stake"])}
        self.add_inputs = [
            ttk.Combobox(tools, values=STAKES, state="readonly", width=8, textvariable=self.add_vars["stake"]),
            ttk.Combobox(tools, values=DECKS, state="readonly", width=11, textvariable=self.add_vars["deck"]),
        ]
        for w in self.add_inputs:
            w.pack(side="right", padx=(0, px(6)))
        ttk.Label(tools, text="INSTANCES", style="Hint.TLabel").pack(side="left")

        self.footer = tk.StringVar()
        ttk.Label(parent, textvariable=self.footer, style="Hint.TLabel").pack(
            side="bottom", fill="x", padx=px(12), pady=(0, px(8)))

        body = ttk.Frame(parent)
        body.pack(fill="both", expand=True, padx=px(12), pady=(px(6), px(10)))
        self.feed = RecentGames(body, self.tooltip, width=px(210))
        self.feed.pack(side="right", fill="y", padx=(px(8), 0))
        self.table = InstanceTable(body, self.tooltip, self.toggle_explore, self.remove_instance)
        self.table.pack(side="left", fill="both", expand=True)
        self._sync_inputs()

    def _build_mini(self, parent: ttk.Frame) -> None:
        head = tk.Frame(parent, bg=BG)
        head.pack(fill="x", padx=px(10), pady=(px(8), px(2)))
        tk.Label(head, text="JackPotts", bg=BG, fg=GOLD, font=(FONT, 12, "bold")).pack(side="left")
        expand = ttk.Button(head, text="⤢", style="Icon.TButton", width=2, command=self.toggle_compact)
        expand.pack(side="right")
        self._tip(expand, "Full view")
        self.mini_lines = []
        for _ in range(3):
            row = tk.Frame(parent, bg=BG)
            row.pack(fill="x", padx=px(10))
            dot = tk.Label(row, text="●", bg=BG, fg=DIM, font=(FONT, 9))
            dot.pack(side="left")
            text = tk.StringVar()
            tk.Label(row, textvariable=text, bg=BG, fg=TEXT, font=(FONT, 10), anchor="w").pack(
                side="left", padx=(px(6), 0))
            self.mini_lines.append((dot, text))

    def _tip(self, widget: tk.Widget, text) -> None:
        """Hover text for a widget; text may be a function, for tips that follow the panel's state."""
        widget.bind("<Enter>", lambda e: self.tooltip.show(text() if callable(text) else text,
                                                           e.x_root, e.y_root))
        widget.bind("<Leave>", lambda e: self.tooltip.hide())

    def _sync_inputs(self) -> None:
        """Inputs lock while a batch runs; the rate only matters when something explores."""
        try:
            exploring = int(self.vars["explore_instances"].get()) > 0
        except (tk.TclError, ValueError):
            exploring = True
        for w in self.inputs:
            off = self.running or (w is self.explore_rate_input and not exploring)
            w.state(["disabled"] if off else ["!disabled"])

    def toggle_compact(self) -> None:
        self.compact = not self.compact
        self.tooltip.hide()
        if self.compact:
            self.full_geometry = self.root.geometry()
            self.full.pack_forget()
            self.mini.pack(fill="both", expand=True)
            self.root.minsize(px(380), px(120))
            self.root.geometry(f"{px(440)}x{px(124)}")
            self.root.attributes("-topmost", True)
        else:
            self.mini.pack_forget()
            self.full.pack(fill="both", expand=True)
            self.root.attributes("-topmost", False)
            self.root.minsize(px(FULL_MIN[0]), px(FULL_MIN[1]))
            self.root.geometry(self.full_geometry)
        self.refresh(reschedule=False)

    # ── Actions ──

    def _settings(self) -> dict:
        try:
            settings = {
                "instances": int(self.vars["instances"].get()),
                "games": int(self.vars["games"].get()),
                "deck": self.vars["deck"].get(),
                "stake": self.vars["stake"].get(),
                "explore_instances": int(self.vars["explore_instances"].get()),
                "explore_rate": round(float(self.vars["explore_rate"].get()), 2),
                "sim": bool(self.vars["sim"].get()),
                "kernel": bool(self.vars["kernel"].get()),
            }
        except (tk.TclError, ValueError):
            raise ValueError("Instances, games and exploring must be whole numbers.")
        if not 1 <= settings["instances"] <= MAX_INSTANCES:
            raise ValueError(f"Instances must be 1 to {MAX_INSTANCES}.")
        if settings["games"] < 1:
            raise ValueError("Games each must be at least 1.")
        if not 0 <= settings["explore_instances"] <= settings["instances"]:
            raise ValueError("Exploring can't be more than the number of instances.")
        if not 0 < settings["explore_rate"] <= 1:
            raise ValueError("Explore rate must be above 0 and at most 1.")
        return settings

    def _launched(self, pending: str) -> None:
        self.stop_requested = self.pause_requested = False
        self.pending = pending
        self.launched_at = time.time()
        self.refresh(reschedule=False)

    def start(self) -> None:
        """Start a batch, or resume the paused one."""
        if read_status() is not None or ports_in_use():
            return
        if self.train.playing and not self._overlap_ok("A learning run"):
            return
        if self.stream.phase in ("starting", "running", "stopping") and not self._overlap_ok("The stream"):
            return
        if paused_status(read_status_file()):
            resume_paused_batch()
            self._launched("starting")
            return
        try:
            settings = self._settings()
        except ValueError as e:
            messagebox.showerror("JackPotts", str(e), parent=self.root)
            return
        launch_batch(settings)
        self._launched("starting")

    def stop(self) -> None:
        """Stop the running batch, or end the paused one."""
        if read_status() is None:
            if paused_status(read_status_file()) and messagebox.askyesno(
                    "JackPotts", "End the paused batch? It is marked finished and stats run.",
                    parent=self.root):
                resume_paused_batch(finish=True)
                self._launched("stopping")
            return
        if not messagebox.askyesno("JackPotts", "Stop the batch? Games in progress are dropped.",
                                   parent=self.root):
            return
        STOP_FILE.touch()
        self.stop_requested = True
        self.refresh(reschedule=False)

    def pause(self) -> None:
        """Close every game but keep the batch open, so Resume picks up the same counts."""
        if read_status() is None:
            return
        PAUSE_FILE.touch()
        self.pause_requested = True
        self.refresh(reschedule=False)

    def add_instance(self) -> None:
        send_command({"op": "add", "deck": self.add_vars["deck"].get(), "stake": self.add_vars["stake"].get()})
        self.add_btn.state(["disabled"])

    def remove_instance(self, slot: dict) -> None:
        if not messagebox.askyesno("JackPotts", f"Remove {slot['name']}? Its game in progress is dropped.",
                                   parent=self.root):
            return
        send_command({"op": "remove", "port": slot["port"]})

    def toggle_explore(self, slot: dict) -> None:
        """Flip an instance between greedy and exploring, from its next game on."""
        if slot["explore_rate"] > 0:
            rate = 0.0
        else:
            rate = (self.table.status or {}).get("explore_rate") or load_settings()["explore_rate"]
        try:
            set_explore_rate(slot["port"], rate)
        except OSError as e:
            messagebox.showerror("JackPotts", f"Could not set the explore rate: {e}", parent=self.root)
            return
        slot["explore_rate"] = rate
        self.table.redraw()

    def _overlap_ok(self, other: str) -> bool:
        return messagebox.askyesno("JackPotts", f"{other} is running; both will be slower. Start anyway?",
                                   parent=self.root)

    # ── Refresh ──

    def _watch_for_finish(self, status: dict | None, final: dict | None) -> None:
        """Notify once when the batch this window was watching ends or pauses."""
        if status is not None:
            self.watched_session = status.get("session")
            return
        if self.watched_session is None:
            return
        if final and final.get("session") == self.watched_session:
            verb = "paused" if paused_status(final) else "ended"
            self.tray.notify(f"Batch #{self.watched_session} {verb}", batch_summary(final))
        self.watched_session = None

    def _phase(self, status: dict | None, launching: bool, outside: bool) -> str:
        if status is not None:
            if status["phase"] == "stopping" or self.stop_requested:
                return "stopping"
            if status["phase"] == "pausing" or self.pause_requested:
                return "pausing"
            return "starting" if status["phase"] == "starting" else "running"
        if launching:
            return self.pending
        return "paused" if self.paused else "outside" if outside else "idle"

    def refresh(self, reschedule: bool = True) -> None:
        status = read_status()
        last = status or read_status_file()
        # A just-launched supervisor gets a minute to write its first status.
        launching = (status is None and time.time() - self.launched_at < 60
                     and (last or {}).get("updated", 0) < self.launched_at)
        self.paused = paused_status(last) if status is None and not launching else None
        outside = status is None and not launching and not self.paused and ports_in_use()
        self.running = status is not None or launching or outside or self.paused is not None
        self._watch_for_finish(status, last)

        phase = self._phase(status, launching, outside)

        can_start = phase in ("idle", "paused")
        can_pause = phase in ("running", "starting")
        can_stop = phase in ("running", "starting", "paused")
        for btn, on in ((self.start_btn, can_start), (self.pause_btn, can_pause), (self.stop_btn, can_stop)):
            btn.state(["!disabled"] if on else ["disabled"])
        self.start_btn.configure(text="▶  Resume" if phase == "paused" else "▶  Start")
        slots = status["slots"] if status else []
        live = [sl for sl in slots if sl["state"] not in ("done", "removed")]
        can_add = phase == "running" and len(live) < MAX_INSTANCES and len(slots) < PORT_SPAN
        for w in (self.add_btn, *self.add_inputs):
            w.state(["!disabled"] if can_add else ["disabled"])
        self._sync_inputs()
        if not self.running:
            self.stop_requested = False
        if status is None:
            self.pause_requested = False

        shown = status or self.paused
        self.table.empty_text = {"starting": "Starting…", "stopping": "Stopping…",
                                 "outside": "Running outside this panel"}.get(phase, "No batch running")
        self.table.update_status(shown)
        self.feed.update_summary(shown.get("recent") if shown else None,
                                 shown.get("recent_wins") if shown else None)
        self._fill_stats(shown)
        self.footer.set({
            "starting": "Starting the supervisor…",
            "outside": "A batch started outside this panel holds the instance ports. Stop it there first.",
            "stopping": "Stopping: closing games, uploading results and running stats…",
            "pausing": "Pausing: closing games. Counts are kept.",
            "paused": "Paused. Resume carries on with the same counts; Stop ends the batch.",
        }.get(phase, "Closing hides the panel to the tray. Batches keep running."))
        self.train.refresh()
        self.stream.refresh()
        self._fill_header(phase, shown)

        if reschedule:
            self.root.after(REFRESH_MS, self.refresh)

    def _fill_stats(self, status: dict | None) -> None:
        self.games_bar.delete("all")
        if status is None:
            for key, v in self.stat_vals.items():
                v.set("—")
                self.stat_labels[key].configure(fg=DIM)
            self.win_pct.set("")
            return
        for lbl in self.stat_labels.values():
            lbl.configure(fg=GOLD)
        completed, expected, wins = status["completed"], status["total_expected"], status["wins"]
        self.stat_vals["batch"].set(f"#{status['session']}")
        self.stat_vals["games"].set(f"{completed:,} / {expected:,}")
        self.stat_vals["wins"].set(f"{wins:,}")
        self.win_pct.set(_win_pct(wins, completed))
        self.stat_labels["wins"].configure(fg=GOLD if wins else DIM)
        self.stat_vals["rate"].set(f"{status['rate_per_hour']:.0f}" if status["rate_per_hour"] else "—")
        self.stat_vals["eta"].set(_fmt_eta(status["eta_secs"]))
        self._bar(self.games_bar, completed / expected if expected else 0)

    @staticmethod
    def _bar(canvas: tk.Canvas, frac: float) -> None:
        canvas.update_idletasks()
        w, h = canvas.winfo_width(), px(4)
        canvas.create_rectangle(0, 0, w, h, fill=BORDER, width=0)
        if frac > 0:
            canvas.create_rectangle(0, 0, max(px(2), int(w * min(frac, 1))), h, fill=GOLD, width=0)

    def _fill_header(self, batch_phase: str, batch_status: dict | None) -> None:
        """Each tab's dot, the strip in the shown tab's colour, and the compact view."""
        colours = {
            "train": self.train.mini_line()[0],
            "stream": self.stream.mini_line()[0],
            "batches": PHASE_COLOURS[batch_phase] if batch_phase != "idle" else DIM,
        }
        for key, (_label, dot, _line) in self.tab_widgets.items():
            dot.configure(fg=colours[key])
        shown = colours[self.tab]
        self.strip.configure(bg=shown if shown != DIM else BORDER)
        if not self.compact:
            return
        if batch_status is not None:
            batch = (PHASE_COLOURS[batch_phase], f"Batch #{batch_status['session']}   "
                     f"{batch_status['completed']:,} / {batch_status['total_expected']:,}   "
                     f"{batch_status['wins']} wins")
        else:
            batch = (PHASE_COLOURS[batch_phase] if batch_phase != "idle" else DIM,
                     {"idle": "No batch"}.get(batch_phase, f"Batch {batch_phase}"))
        for (dot, text), (colour, line) in zip(self.mini_lines,
                                               (self.train.mini_line(), self.stream.mini_line(), batch)):
            dot.configure(fg=colour)
            text.set(line)


def main() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass
    if "--start" in sys.argv[1:]:
        start_saved_batch()
    if show_running_panel():
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except (AttributeError, OSError):
        pass
    root = tk.Tk()
    root.iconbitmap(default=str(ICON_FILE))
    ControlPanel(root)
    root.mainloop()


if __name__ == "__main__":
    main()
