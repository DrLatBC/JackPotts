"""Desktop control panel for batch runs: start, watch and stop the supervisor.

The supervisor runs as its own windowless process, so closing this window
never stops a batch; reopening it picks the running batch back up from the
status file the supervisor writes every tick. Stop asks for a clean
shutdown through the supervisor's stop file. Exploration is switched per
instance by rewriting the rate file its bot re-reads before each game.

Launch with the repo's .venv pythonw (no console):
    .venv\\Scripts\\pythonw.exe -m balatro_bot.gui
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import time
import tkinter as tk
import webbrowser
from ctypes import wintypes
from pathlib import Path
from tkinter import font as tkfont
from tkinter import messagebox, ttk

from balatro_bot.supervisor import BASE_DIR, BASE_PORT, LOG_DIR, PYTHON, STATUS_FILE, STOP_FILE

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
            "explore_instances": 2, "explore_rate": 0.15}

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
PHASE_COLOURS = {"running": GREEN, "stopping": YELLOW, "starting": BLUE, "outside": PURPLE, "idle": BORDER}

FONT = "Segoe UI"

# Screen pixels per 96-dpi pixel; set once the window exists. Sizes below are
# written at 96 dpi and pass through px() so the panel stays sharp and in
# proportion on scaled displays.
_scale = 1.0


def px(n: float) -> int:
    return int(round(n * _scale))


def _pid_alive(pid: int) -> bool:
    """True when a process with this pid is still running (Windows)."""
    process_query_limited_information = 0x1000
    still_active = 259
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return False
    code = ctypes.c_ulong()
    ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
    kernel32.CloseHandle(handle)
    return bool(ok) and code.value == still_active


def read_status_file() -> dict | None:
    """The status file as last written, whether or not its supervisor is alive."""
    try:
        return json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def read_status() -> dict | None:
    """The supervisor's latest status, or None when no supervisor is alive."""
    status = read_status_file()
    if status is None or status.get("phase") == "stopped" or not _pid_alive(int(status.get("pid", 0))):
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


def ports_in_use(count: int = 8) -> bool:
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


def load_settings() -> dict:
    try:
        saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    return {**DEFAULTS, **{k: v for k, v in saved.items() if k in DEFAULTS}}


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
    return cmd


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


def _win_pct(wins: int, games: int) -> str:
    return f"{100 * wins / games:.1f}%" if games else ""


# ── Windows notification ──

class _NotifyIconData(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256), ("uTimeout", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64), ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", wintypes.HICON),
    ]


def notify(root: tk.Tk, title: str, message: str) -> None:
    """Show a Windows notification, removing its tray icon a little later."""
    nif_icon, nif_tip, nif_info = 0x2, 0x4, 0x10
    nim_add, nim_delete = 0, 2
    shell32, user32 = ctypes.windll.shell32, ctypes.windll.user32
    user32.LoadIconW.restype = wintypes.HICON
    user32.GetParent.restype = wintypes.HWND
    data = _NotifyIconData()
    data.cbSize = ctypes.sizeof(_NotifyIconData)
    data.hWnd = user32.GetParent(root.winfo_id()) or root.winfo_id()
    data.uID = 1
    data.uFlags = nif_icon | nif_tip | nif_info
    data.hIcon = user32.LoadIconW(None, ctypes.c_void_p(32516))  # IDI_INFORMATION
    data.szTip = "JackPotts"
    data.szInfoTitle = title[:63]
    data.szInfo = message[:255]
    data.dwInfoFlags = 0x1  # NIIF_INFO
    if not shell32.Shell_NotifyIconW(nim_add, ctypes.byref(data)):
        root.bell()
        return
    root.after(15000, lambda: shell32.Shell_NotifyIconW(nim_delete, ctypes.byref(data)))


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

    # key, heading, width at 96 dpi (the last column takes what's left)
    COLUMNS = [
        ("dot", "", 24), ("name", "NAME", 86), ("deck", "DECK", 70), ("explore", "EXPLORE", 80),
        ("progress", "PROGRESS", 146), ("wins", "WINS", 80), ("game", "NOW", 140),
        ("idle", "IDLE", 46), ("issue", "LAST ISSUE", 0),
    ]

    def __init__(self, master: tk.Misc, tooltip: Tooltip, on_explore, **kw) -> None:
        super().__init__(master, bg=CARD, highlightthickness=0, bd=0, **kw)
        self.head_h, self.row_h = px(26), px(32)
        self.tooltip = tooltip
        self.on_explore = on_explore
        self.status: dict | None = None
        self.empty_text = "No batch running"
        self.hover_row: int | None = None
        self.bold = tkfont.Font(family=FONT, size=10, weight="bold")
        self.head_font = tkfont.Font(family=FONT, size=8, weight="bold")
        self.small = tkfont.Font(family=FONT, size=9)
        self.bind("<Configure>", lambda e: self.redraw())
        self.bind("<Motion>", self._motion)
        self.bind("<Leave>", self._leave)
        self.bind("<Button-1>", self._click)
        self.bind("<Double-Button-1>", self._double)

    def update_status(self, status: dict | None) -> None:
        self.status = status
        self.redraw()

    @property
    def slots(self) -> list[dict]:
        return self.status["slots"] if self.status else []

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
        row = (y - self.head_h) // self.row_h
        if row >= len(self.slots):
            return None, None
        for key, (x0, x1) in self._spans().items():
            if x0 <= x < x1:
                return row, key
        return row, None

    def explore_toggleable(self) -> bool:
        """Only a supervisor that hands its bots a rate file can switch them mid-batch."""
        return bool(self.status and self.status.get("phase") == "running"
                    and self.slots and "explore_active" in self.slots[0])

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
        return ""

    def _motion(self, e: tk.Event) -> None:
        row, key = self._hit(e.x, e.y)
        if row != self.hover_row:
            self.hover_row = row
            self.redraw()
        self.configure(cursor="hand2" if key == "explore" and self.explore_toggleable() else "")
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

    def _double(self, e: tk.Event) -> None:
        row, key = self._hit(e.x, e.y)
        if row is None or key == "explore":
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
        stall = (self.status or {}).get("stall_secs", 600)
        for i, slot in enumerate(self.slots):
            self._draw_row(i, slot, spans, stall)

    def _draw_row(self, i: int, s: dict, spans: dict, stall: int) -> None:
        top = self.head_h + i * self.row_h
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

        ante, blind = s.get("ante", 0), s.get("blind", "")
        if ante:
            self.create_text(x["game"], mid, text=f"A{ante}", anchor="w", fill=GOLD, font=self.bold)
            self.create_text(x["game"] + px(30), mid, text=self._fit(blind, w["game"] - px(30), self.small),
                             anchor="w", fill=TEXT, font=self.small)
        else:
            self.create_text(x["game"], mid, text="—", anchor="w", fill=DIM, font=self.small)

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
    """The latest finished games across every instance, newest first."""

    def __init__(self, master: tk.Misc, tooltip: Tooltip, **kw) -> None:
        super().__init__(master, bg=CARD, highlightthickness=0, bd=0, **kw)
        self.head_h, self.row_h = px(26), px(26)
        self.tooltip = tooltip
        self.games: list[dict] = []
        self.font = tkfont.Font(family=FONT, size=10)
        self.bold = tkfont.Font(family=FONT, size=10, weight="bold")
        self.small = tkfont.Font(family=FONT, size=9)
        self.bind("<Configure>", lambda e: self.redraw())
        self.bind("<Motion>", self._motion)
        self.bind("<Leave>", lambda e: self.tooltip.hide())

    def update_games(self, games: list[dict]) -> None:
        self.games = games
        self.redraw()

    def _motion(self, e: tk.Event) -> None:
        row = (e.y - self.head_h) // self.row_h
        if e.y >= self.head_h and row < len(self.games):
            g = self.games[row]
            self.tooltip.show(f"{'Win' if g['won'] else 'Loss'} at ante {g['ante']} · seed {g['seed']}",
                              e.x_root, e.y_root)
        else:
            self.tooltip.hide()

    def redraw(self) -> None:
        self.delete("all")
        width = self.winfo_width()
        self.create_text(px(12), self.head_h // 2 + px(2), text="RECENT GAMES", anchor="w", fill=MUTED,
                         font=(FONT, 8, "bold"))
        self.create_line(px(8), self.head_h, width - px(8), self.head_h, fill=BORDER)
        if not self.games:
            self.create_text(width // 2, self.head_h + px(34), text="None yet", fill=DIM, font=(FONT, 10))
            return
        now = time.time()
        r = px(4)
        for i, g in enumerate(self.games):
            mid = self.head_h + i * self.row_h + self.row_h // 2
            if mid + self.row_h // 2 > self.winfo_height():
                break
            won = g["won"]
            self.create_oval(px(14), mid - r, px(14) + 2 * r, mid + r, fill=GOLD if won else DIM, outline="")
            self.create_text(px(32), mid, text=g["name"], anchor="w", fill=GOLD if won else TEXT,
                             font=self.bold if won else self.font)
            self.create_text(width - px(56), mid, text=f"A{g['ante']}", anchor="e",
                             fill=GOLD if won else MUTED, font=self.small)
            self.create_text(width - px(12), mid, text=_fmt_secs(now - g["time"]), anchor="e",
                             fill=DIM, font=self.small)


class ControlPanel:
    def __init__(self, root: tk.Tk) -> None:
        global _scale
        _scale = root.winfo_fpixels("1i") / 96
        self.root = root
        self.stop_requested = False
        self.launched_at = 0.0
        self.running = False
        self.compact = False
        self.full_geometry = f"{px(1180)}x{px(600)}"
        self.watched_session: int | None = None
        root.title("JackPotts")
        root.configure(bg=BG)
        root.minsize(px(1000), px(520))
        root.geometry(self.full_geometry)
        self._style()
        self.tooltip = Tooltip(root)
        self._build()
        self.refresh()

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
        style.configure("TSpinbox", arrowcolor=TEXT, foreground=TEXT, fieldbackground=BG,
                        background=CARD, arrowsize=px(11))
        style.map("TSpinbox", foreground=[("disabled", DIM)], fieldbackground=[("disabled", CARD)],
                  arrowcolor=[("disabled", DIM)])
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
        self.root.option_add("*TCombobox*Listbox.background", CARD)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", BORDER)

    def _build(self) -> None:
        self.strip = tk.Frame(self.root, bg=BORDER, height=px(3))
        self.strip.pack(fill="x", side="top")
        self.full = ttk.Frame(self.root)
        self.full.pack(fill="both", expand=True)
        self._build_full(self.full)
        self.mini = ttk.Frame(self.root)
        self._build_mini(self.mini)

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
        }
        self.explore_rate_input = ttk.Spinbox(controls, from_=0.05, to=1.0, increment=0.05, width=5,
                                              format="%.2f", textvariable=self.vars["explore_rate"])
        fields = [
            ("INSTANCES", ttk.Spinbox(controls, from_=1, to=8, width=3, textvariable=self.vars["instances"])),
            ("GAMES EACH", ttk.Spinbox(controls, from_=1, to=100000, increment=100, width=7,
                                       textvariable=self.vars["games"])),
            ("DECK", ttk.Combobox(controls, values=DECKS, state="readonly", width=11,
                                  textvariable=self.vars["deck"])),
            ("STAKE", ttk.Combobox(controls, values=STAKES, state="readonly", width=8,
                                   textvariable=self.vars["stake"])),
            ("EXPLORING", ttk.Spinbox(controls, from_=0, to=8, width=3,
                                      textvariable=self.vars["explore_instances"])),
            ("RATE", self.explore_rate_input),
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
        self.start_btn = ttk.Button(buttons, text="▶  Start", style="Start.TButton", command=self.start)
        self.start_btn.pack(side="left", fill="y")
        self.stop_btn = ttk.Button(buttons, text="■  Stop", style="Stop.TButton", command=self.stop)
        self.stop_btn.pack(side="left", fill="y", padx=(px(8), 0))

        links = ttk.Frame(top)
        links.pack(side="right", fill="y")
        for text, tip, cmd in (("⤡", "Compact", self.toggle_compact),
                               ("☰", "Logs folder", lambda: os.startfile(LOG_DIR)),
                               ("↗", "Dashboard", lambda: webbrowser.open(DASHBOARD_URL))):
            b = ttk.Button(links, text=text, style="Icon.TButton", width=2, command=cmd)
            b.pack(side="right", fill="y", padx=(px(6), 0))
            self._tip(b, tip)

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

        self.footer = tk.StringVar()
        ttk.Label(parent, textvariable=self.footer, style="Hint.TLabel").pack(
            side="bottom", fill="x", padx=px(12), pady=(0, px(8)))

        body = ttk.Frame(parent)
        body.pack(fill="both", expand=True, padx=px(12), pady=px(10))
        self.feed = RecentGames(body, self.tooltip, width=px(210))
        self.feed.pack(side="right", fill="y", padx=(px(8), 0))
        self.table = InstanceTable(body, self.tooltip, self.toggle_explore)
        self.table.pack(side="left", fill="both", expand=True)
        self._sync_inputs()

    def _build_mini(self, parent: ttk.Frame) -> None:
        head = tk.Frame(parent, bg=BG)
        head.pack(fill="x", padx=px(10), pady=(px(8), 0))
        self.mini_title = tk.StringVar(value="—")
        tk.Label(head, textvariable=self.mini_title, bg=BG, fg=GOLD,
                 font=(FONT, 13, "bold")).pack(side="left")
        expand = ttk.Button(head, text="⤢", style="Icon.TButton", width=2, command=self.toggle_compact)
        expand.pack(side="right")
        self._tip(expand, "Full view")
        self.mini_bar = tk.Canvas(parent, bg=BG, height=px(4), highlightthickness=0, bd=0)
        self.mini_bar.pack(fill="x", padx=px(10), pady=(px(6), px(4)))
        self.mini_line = tk.StringVar()
        tk.Label(parent, textvariable=self.mini_line, bg=BG, fg=TEXT,
                 font=(FONT, 10)).pack(anchor="w", padx=px(10))
        self.mini_dots = tk.Canvas(parent, bg=BG, height=px(20), highlightthickness=0, bd=0)
        self.mini_dots.pack(fill="x", padx=px(10), pady=(px(4), px(8)))

    def _tip(self, widget: tk.Widget, text: str) -> None:
        widget.bind("<Enter>", lambda e: self.tooltip.show(text, e.x_root, e.y_root))
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
            self.root.minsize(px(300), px(110))
            self.root.geometry(f"{px(340)}x{px(118)}")
            self.root.attributes("-topmost", True)
        else:
            self.mini.pack_forget()
            self.full.pack(fill="both", expand=True)
            self.root.attributes("-topmost", False)
            self.root.minsize(px(1000), px(520))
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
            }
        except (tk.TclError, ValueError):
            raise ValueError("Instances, games and exploring must be whole numbers.")
        if not 1 <= settings["instances"] <= 8:
            raise ValueError("Instances must be 1 to 8.")
        if settings["games"] < 1:
            raise ValueError("Games each must be at least 1.")
        if not 0 <= settings["explore_instances"] <= settings["instances"]:
            raise ValueError("Exploring can't be more than the number of instances.")
        if not 0 < settings["explore_rate"] <= 1:
            raise ValueError("Explore rate must be above 0 and at most 1.")
        return settings

    def start(self) -> None:
        if read_status() is not None or ports_in_use():
            return
        try:
            settings = self._settings()
        except ValueError as e:
            messagebox.showerror("JackPotts", str(e), parent=self.root)
            return
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(settings), encoding="utf-8")
        STOP_FILE.unlink(missing_ok=True)
        STATUS_FILE.unlink(missing_ok=True)
        with open(CONSOLE_LOG, "w", encoding="utf-8") as out:
            subprocess.Popen(
                supervisor_command(settings),
                cwd=str(BASE_DIR),
                stdout=out,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        self.stop_requested = False
        self.launched_at = time.time()
        self.refresh(reschedule=False)

    def stop(self) -> None:
        if read_status() is None:
            return
        if not messagebox.askyesno("JackPotts", "Stop the batch? Games in progress are dropped.",
                                   parent=self.root):
            return
        STOP_FILE.touch()
        self.stop_requested = True
        self.refresh(reschedule=False)

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

    # ── Refresh ──

    def _watch_for_finish(self, status: dict | None) -> None:
        """Notify once when the batch this window was watching ends."""
        if status is not None:
            self.watched_session = status.get("session")
            return
        if self.watched_session is None:
            return
        final = read_status_file()
        if final and final.get("session") == self.watched_session:
            notify(self.root, f"Batch #{self.watched_session} ended", batch_summary(final))
        self.watched_session = None

    def refresh(self, reschedule: bool = True) -> None:
        status = read_status()
        starting = status is None and time.time() - self.launched_at < 60
        outside = status is None and not starting and ports_in_use()
        self.running = status is not None or starting or outside
        self._watch_for_finish(status)

        if status is not None:
            phase = ("stopping" if status["phase"] == "stopping" or self.stop_requested
                     else "starting" if status["phase"] == "starting" else "running")
        else:
            phase = "starting" if starting else "outside" if outside else "idle"
        self.strip.configure(bg=PHASE_COLOURS[phase])

        self.start_btn.state(["disabled"] if self.running else ["!disabled"])
        self.stop_btn.state(["!disabled"] if status is not None and phase != "stopping" else ["disabled"])
        self._sync_inputs()
        if not self.running:
            self.stop_requested = False

        self.table.empty_text = {"starting": "Starting…", "outside": "Running outside this panel"}.get(
            phase, "No batch running")
        self.table.update_status(status)
        self.feed.update_games(status.get("recent_games", []) if status else [])
        self._fill_stats(status)
        self._fill_mini(status, phase)
        self.footer.set({
            "starting": "Starting the supervisor…",
            "outside": "A batch started outside this panel holds the instance ports. Stop it there first.",
            "stopping": "Stopping: closing games, uploading results and running stats…",
        }.get(phase, "Closing this window never stops a batch."))

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

    def _fill_mini(self, status: dict | None, phase: str) -> None:
        self.mini_bar.delete("all")
        self.mini_dots.delete("all")
        if not self.compact:
            return
        if status is None:
            self.mini_title.set({"starting": "Starting…", "outside": "Outside batch"}.get(phase, "Idle"))
            self.mini_line.set("")
            self._bar(self.mini_bar, 0)
            return
        completed, expected = status["completed"], status["total_expected"]
        self.mini_title.set(f"#{status['session']}   {completed:,} / {expected:,}")
        self.mini_line.set(f"{status['wins']} wins  {_win_pct(status['wins'], completed)}"
                           f"   ·   {_fmt_eta(status['eta_secs'])}")
        self._bar(self.mini_bar, completed / expected if expected else 0)
        step, d = px(18), px(12)
        for i, s in enumerate(status["slots"]):
            x = i * step + px(2)
            fill = STATE_COLOURS.get(s["state"], MUTED)
            outline = PURPLE if s.get("explore_active", s["explore_rate"]) else fill
            self.mini_dots.create_oval(x, px(4), x + d, px(4) + d, fill=fill, outline=outline, width=px(2))


def main() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass
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
