"""Desktop control panel for batch runs: start, watch and stop the supervisor.

The supervisor runs as its own windowless process, so closing this window
never stops a batch; reopening it picks the running batch back up from the
status file the supervisor writes every tick. Stop asks for a clean
shutdown through the supervisor's stop file.

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
from tkinter import messagebox, ttk

from balatro_bot.supervisor import BASE_DIR, BASE_PORT, LOG_DIR, PYTHON, STATUS_FILE, STOP_FILE

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
BORDER = "#2a3550"
TEXT = "#e0e0e0"
MUTED = "#8892a4"
GOLD = "#d4a843"
GREEN = "#4caf50"
RED = "#e74c3c"
BLUE = "#3498db"
PURPLE = "#9b59b6"
YELLOW = "#f1c40f"

STATE_COLOURS = {
    "running": GREEN, "starting": BLUE, "restarting": YELLOW,
    "cooldown": RED, "dead": RED, "done": MUTED, "stopped": MUTED,
}


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


def read_status() -> dict | None:
    """The supervisor's latest status, or None when no supervisor is alive."""
    try:
        status = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if status.get("phase") == "stopped" or not _pid_alive(int(status.get("pid", 0))):
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


def _fmt_eta(secs: int | None) -> str:
    if secs is None:
        return "—"
    h, m = divmod(secs // 60, 60)
    return f"~{h}h{m:02d}m"


class ControlPanel:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.stop_requested = False
        root.title("JackPotts")
        root.configure(bg=BG)
        root.minsize(1000, 520)
        self._style()
        self._build()
        self.refresh()

    # ── Layout ──

    def _style(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", background=BG, foreground=TEXT, fieldbackground=CARD,
                        bordercolor=BORDER, font=("Segoe UI", 10))
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Muted.TLabel", foreground=MUTED, font=("Segoe UI", 9))
        style.configure("Stat.TLabel", foreground=GOLD, font=("Segoe UI", 13, "bold"))
        style.configure("StatName.TLabel", foreground=MUTED, font=("Segoe UI", 8))
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD)
        style.configure("Card.TLabel", background=CARD)
        style.configure("TSpinbox", arrowcolor=TEXT, foreground=TEXT)
        style.configure("TCombobox", arrowcolor=TEXT, foreground=TEXT)
        style.map("TCombobox", fieldbackground=[("readonly", CARD)],
                  foreground=[("disabled", MUTED), ("readonly", TEXT)])
        style.configure("TButton", background=CARD, foreground=TEXT, padding=(12, 5))
        style.map("TButton", background=[("active", BORDER), ("disabled", BG)],
                  foreground=[("disabled", MUTED)])
        style.configure("Start.TButton", foreground=GREEN)
        style.configure("Stop.TButton", foreground=RED)
        style.configure("Treeview", background=CARD, fieldbackground=CARD, foreground=TEXT,
                        rowheight=26, borderwidth=0)
        style.configure("Treeview.Heading", background=BG, foreground=MUTED,
                        font=("Segoe UI", 8, "bold"), borderwidth=0)
        style.map("Treeview", background=[("selected", BORDER)])
        self.root.option_add("*TCombobox*Listbox.background", CARD)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT)

    def _build(self) -> None:
        pad = {"padx": 12, "pady": 6}
        s = load_settings()

        controls = ttk.Frame(self.root)
        controls.pack(fill="x", **pad)
        self.vars = {
            "instances": tk.IntVar(value=s["instances"]),
            "games": tk.IntVar(value=s["games"]),
            "deck": tk.StringVar(value=s["deck"]),
            "stake": tk.StringVar(value=s["stake"]),
            "explore_instances": tk.IntVar(value=s["explore_instances"]),
            "explore_rate": tk.DoubleVar(value=s["explore_rate"]),
        }
        fields = [
            ("Instances", ttk.Spinbox(controls, from_=1, to=8, width=4, textvariable=self.vars["instances"])),
            ("Games each", ttk.Spinbox(controls, from_=1, to=100000, increment=100, width=7,
                                       textvariable=self.vars["games"])),
            ("Deck", ttk.Combobox(controls, values=DECKS, state="readonly", width=11,
                                  textvariable=self.vars["deck"])),
            ("Stake", ttk.Combobox(controls, values=STAKES, state="readonly", width=8,
                                   textvariable=self.vars["stake"])),
            ("Exploring", ttk.Spinbox(controls, from_=0, to=8, width=4,
                                      textvariable=self.vars["explore_instances"])),
            ("Explore rate", ttk.Spinbox(controls, from_=0.05, to=1.0, increment=0.05, width=5,
                                         format="%.2f", textvariable=self.vars["explore_rate"])),
        ]
        self.inputs = []
        for col, (label, widget) in enumerate(fields):
            ttk.Label(controls, text=label, style="Muted.TLabel").grid(row=0, column=col, sticky="w", padx=(0, 10))
            widget.grid(row=1, column=col, sticky="w", padx=(0, 10))
            self.inputs.append(widget)

        buttons = ttk.Frame(self.root)
        buttons.pack(fill="x", padx=12)
        self.start_btn = ttk.Button(buttons, text="▶  Start", style="Start.TButton", command=self.start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(buttons, text="■  Stop", style="Stop.TButton", command=self.stop)
        self.stop_btn.pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="Dashboard", command=lambda: webbrowser.open(DASHBOARD_URL)).pack(side="right")
        ttk.Button(buttons, text="Logs", command=lambda: os.startfile(LOG_DIR)).pack(side="right", padx=(0, 8))

        stats = ttk.Frame(self.root)
        stats.pack(fill="x", padx=12, pady=(12, 6))
        self.stat_vals: dict[str, tk.StringVar] = {}
        for col, (key, name) in enumerate((("batch", "BATCH"), ("games", "GAMES"), ("wins", "WINS"),
                                           ("rate", "GAMES / HOUR"), ("eta", "ETA"))):
            stats.columnconfigure(col, weight=2 if key == "games" else 1, uniform="stat")
            card = ttk.Frame(stats, style="Card.TFrame", padding=(12, 8))
            card.grid(row=0, column=col, sticky="nsew", padx=(0 if col == 0 else 8, 0))
            var = tk.StringVar(value="—")
            self.stat_vals[key] = var
            ttk.Label(card, textvariable=var, style="Stat.TLabel", background=CARD).pack(anchor="w")
            ttk.Label(card, text=name, style="StatName.TLabel", background=CARD).pack(anchor="w")

        cols = ("deck", "progress", "wins", "state", "restarts", "issue")
        heads = ("DECK", "PROGRESS", "WINS", "STATE", "RESTARTS", "LAST ISSUE")
        widths = (140, 110, 60, 110, 90, 220)
        self.tree = ttk.Treeview(self.root, columns=cols, show="tree headings", height=8)
        self.tree.heading("#0", text="NAME", anchor="w")
        self.tree.column("#0", width=140, minwidth=140, anchor="w", stretch=False)
        for c, h, w in zip(cols, heads, widths):
            self.tree.heading(c, text=h, anchor="w")
            self.tree.column(c, width=w, minwidth=w, anchor="w", stretch=(c == "issue"))
        self.tree.tag_configure("explore", foreground=PURPLE)
        for state, colour in STATE_COLOURS.items():
            self.tree.tag_configure(state, foreground=colour)
        self.tree.pack(fill="both", expand=True, padx=12, pady=(6, 6))

        self.footer = tk.StringVar()
        ttk.Label(self.root, textvariable=self.footer, style="Muted.TLabel").pack(
            fill="x", padx=12, pady=(0, 10))

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
        if settings["explore_instances"] and not 0 < settings["explore_rate"] <= 1:
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

    # ── Refresh ──

    def refresh(self, reschedule: bool = True) -> None:
        status = read_status()
        starting = status is None and time.time() - getattr(self, "launched_at", 0) < 60
        outside = status is None and not starting and ports_in_use()
        running = status is not None or starting or outside

        self.start_btn.state(["disabled"] if running else ["!disabled"])
        self.stop_btn.state(["!disabled"] if status is not None and status["phase"] == "running"
                            and not self.stop_requested else ["disabled"])
        for w in self.inputs:
            w.state(["disabled"] if running else ["!disabled"])
        if not running:
            self.stop_requested = False

        self.tree.delete(*self.tree.get_children())
        if status is None:
            for v in self.stat_vals.values():
                v.set("—")
            if starting:
                self.footer.set("Starting the supervisor…")
            elif outside:
                self.footer.set("A batch started outside this panel is running. Stop it there first.")
            else:
                self.footer.set("No batch running.  Closing this window never stops a batch.")
        else:
            completed, expected = status["completed"], status["total_expected"]
            win_pct = f"  {100 * status['wins'] / completed:.1f}%" if completed else ""
            self.stat_vals["batch"].set(f"#{status['session']}")
            self.stat_vals["games"].set(f"{completed} / {expected}")
            self.stat_vals["wins"].set(f"{status['wins']}{win_pct}")
            self.stat_vals["rate"].set(f"{status['rate_per_hour']:.0f}" if status["rate_per_hour"] else "—")
            self.stat_vals["eta"].set(_fmt_eta(status["eta_secs"]))
            for s in status["slots"]:
                deck = s["deck"]
                tags = [s["state"]]
                if s["explore_rate"]:
                    deck = f"{deck}  ◈ {s['explore_rate'] * 100:.0f}%"
                    tags = ["explore"]
                self.tree.insert("", "end", text=s["name"], tags=tags, values=(
                    deck, s["progress"] or "…", s["wins"], s["state"],
                    s["restarts"] or "", s["last_reason"],
                ))
            phase = status["phase"]
            if phase == "stopping" or self.stop_requested:
                self.footer.set("Stopping: closing games, uploading results and running stats…")
            else:
                self.footer.set("Purple rows are exploring.  Closing this window never stops a batch.")

        if reschedule:
            self.root.after(REFRESH_MS, self.refresh)


def main() -> None:
    root = tk.Tk()
    ControlPanel(root)
    root.mainloop()


if __name__ == "__main__":
    main()
