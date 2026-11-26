"""Learning runs and the stream, as files and processes: what the control panel reads,
the commands it launches, and nothing that needs Tk or torch.

Learning runs are ``rl.ppo train`` (``rl/data/ppo/<run>/``). The stream is
``balatro_bot.stream``: the visible game played by a model. Both are jobs
(``balatro_bot.jobs``), which own their status, stop requests and launching.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from balatro_bot import jobs
from balatro_bot.eval_compare import compare
from balatro_bot.jobs import read_json
from balatro_bot.config import REPO, venv_python

RL_PYTHONW = venv_python("rl", windowless=True)
RL_DATA = REPO / "rl" / "data"
# The train flags the panel doesn't show: every run's, then each method's own.
TRAIN_FIXED = ["--fade", "0", "--eval-every", "0.5", "--eval-games", "1000", "--final-check", "5000",
               "--threads", "4"]
METHOD_FIXED = {
    "SAME SEED": ["--games-per-step", "1024", "--plays", "4", "--seeds", "32"],
    "BRANCHES": ["--branch-rate", "0.05"],
}
METHODS = list(METHOD_FIXED)
DECISIONS = 2000
CHANGE = 0.005
ENDLESS = 0.0
OVERKILL = 0.0
STARTS = 0.0
POOL = 200
TRAIN_ACTIVE = jobs.LIVE
RUN_NAME_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")
# A run with no status file whose log changed this recently may still be playing from
# a terminal on code older than the status file; the panel won't offer to resume it.
OUTSIDE_SECS = 600


def run_dir(name: str) -> Path:
    return RL_DATA / "ppo" / name


def copies() -> list[str]:
    """The copies of the bot a run can start from."""
    return sorted(p.stem for p in (RL_DATA / "models").glob("*.pt"))


def valid_run_name(name: str) -> bool:
    return bool(name) and set(name) <= RUN_NAME_CHARS and name.strip(".") != ""


def run_taken(name: str) -> bool:
    """True when a run of this name has finished an update; train refuses to start it again."""
    return valid_run_name(name) and (run_dir(name) / "log.jsonl").exists()


def run_state(name: str) -> dict | None:
    return read_json(run_dir(name) / "state.json") if valid_run_name(name) else None


def resumable(name: str, hours: float | None = None) -> bool:
    """True when the run saved progress and has hours left to play: of its own total,
    or of ``hours`` when a resume gives it a new one."""
    state = run_state(name)
    if state is None or not (run_dir(name) / "latest.pt").exists():
        return False
    total = state["args"]["hours"] if hours is None else hours
    return state["seconds"] < total * 3600


def hours_done(name: str) -> float:
    state = run_state(name)
    return state["seconds"] / 3600 if state else 0.0


# ── What a run starts from ──


def start_sources() -> list[str]:
    """What a new run can start from: each copy, then each run with an update save,
    newest first."""
    sources = model_sources()
    return ([s for s in sources if s.startswith("copy ")]
            + [s for s in sources if s.startswith("run ") and start_saves(s)])


def start_saves(source: str) -> list[str]:
    """A source's saves a run can start from. A run's ``latest`` is left out: it changes
    while that run plays, and the new run is pulled toward its start for as long as it
    plays."""
    return [c for c in checkpoints(source) if c != "latest"]


def start_arg(source: str, save: str) -> str:
    """``--start`` for a choice: a copy's name, or a save's path from the repository root."""
    if source.startswith("copy "):
        return source.split(" ", 1)[1]
    path, _ = model_choice(source, save)
    try:
        return path.relative_to(REPO).as_posix()
    except ValueError:
        return str(path)


def start_choice(start: str) -> tuple[str, str]:
    """The (source, save) a ``--start`` stands for: the inverse of start_arg."""
    if not start.endswith(".pt"):
        return f"copy {start}", start
    return choice_of(start)


def train_command(settings: dict) -> list[str]:
    """A new run: the panel's fields, then the fixed flags of every run and of its method.
    Decisions per step and big starts are a branch run's own fields; the change limit and
    overkill are every run's."""
    method = settings["train_method"]
    branch = (["--decisions-per-step", str(settings.get("train_decisions", DECISIONS)),
               "--start-share", str(settings.get("train_starts", STARTS)),
               "--start-pool", str(settings.get("train_pool", POOL))]
              if method == "BRANCHES" else [])
    return [str(RL_PYTHONW), "-m", "rl.ppo", "train", "--run", settings["train_run"],
            "--start", settings["train_copy"], "--hours", str(settings["train_hours"]),
            "--beta", str(settings["train_beta"]),
            "--target-change", str(settings.get("train_change", CHANGE)), "--win-bonus", str(settings["train_bonus"]),
            "--endless-bonus", str(settings.get("train_endless", ENDLESS)),
            "--overkill", str(settings.get("train_overkill", OVERKILL)),
            "--deck", settings["train_deck"], "--stake", settings["train_stake"],
            *TRAIN_FIXED, *METHOD_FIXED[method], *branch]


def _fixed_args() -> dict:
    """The fixed flags a run from an older panel may lack. Runs from before the method
    choice were all same-seed runs, so those flags fill in; a branch run saved all of its
    own."""
    fixed = TRAIN_FIXED + METHOD_FIXED["SAME SEED"]
    return {flag[2:].replace("-", "_"): value for flag, value in zip(fixed[::2], fixed[1::2])}


def run_settings(args: dict) -> dict:
    """A run's method, deck, stake, win and endless bonuses, overkill, change limit and (a
    branch run's) decisions per step and big starts from the args its state.json saved; a
    run from before those options played same-seed on Blue/White with bonus 8 and stopped
    at its win."""
    branch = bool(args.get("branch_rate"))
    return {"method": "BRANCHES" if branch else "SAME SEED",
            "deck": args.get("deck", "BLUE"), "stake": args.get("stake", "WHITE"),
            "win bonus": args.get("win_bonus", 8.0), "endless bonus": args.get("endless_bonus", ENDLESS), "change": args.get("target_change", CHANGE),
            "overkill": args.get("overkill", OVERKILL),
            "decisions": args.get("decisions_per_step", DECISIONS) if branch else None,
            "start share": args.get("start_share", STARTS) if branch else None,
            "start pool": args.get("start_pool", POOL) if branch else None}


def resume_command(saved_args: dict, hours: float | None = None) -> list[str]:
    """The run's own train command, rebuilt from the args its state.json saved, plus --resume.

    train reads every setting from its command line again on --resume; only progress
    comes from state.json, so the original flags have to be passed back. ``hours`` is a
    new total for the run. A fixed flag the run was started without (a run from an
    older panel, or from a terminal) is added at the panel's value.
    """
    args = {**_fixed_args(), **saved_args}
    if hours is not None:
        args["hours"] = hours
    cmd = [str(RL_PYTHONW), "-m", "rl.ppo", "train"]
    for key, value in args.items():
        if key in ("cmd", "resume"):
            continue
        flag = "--" + key.replace("_", "-")
        if isinstance(value, bool):
            cmd += [flag] if value else []
        else:
            cmd += [flag, str(value)]
    return cmd + ["--resume"]


def read_trainings() -> dict[str, dict]:
    """Every learning run's status by name (``Job.status``: a dead process reads as
    failed, a stop asked as stopping)."""
    return {job.name: status for job in jobs.learning_runs() if (status := job.status()) is not None}


def follow(statuses: dict[str, dict], current: str | None) -> str | None:
    """The run the panel shows: the current one while it plays, else the newest run that
    plays, else the current one, else the newest."""
    def newest(runs):
        return max(runs, key=lambda r: statuses[r].get("updated", 0))

    if current in statuses and statuses[current]["phase"] == "running":
        return current
    playing = [r for r, s in statuses.items() if s["phase"] == "running"]
    if playing:
        return newest(playing)
    if current in statuses or not statuses:
        return current
    return newest(statuses)


def training_summary(status: dict) -> str:
    text = (f"update {status.get('update', 0)}  ·  {status.get('hours', 0):.2f} of "
            f"{status.get('hours total', 0):g} h  ·  {status.get('games', 0):,} games")
    check, ev = status.get("check"), status.get("eval")
    if check:
        text += (f"  ·  {check['seeds']:,}-seed check {check['win rate']:.1%} wins "
                 f"({check['win gain'] * 100:+.1f} ± {check['win gain +/-'] * 100:.1f} on the start)")
    elif ev:
        text += f"  ·  eval {ev['win rate']:.1%} wins, ante {ev['mean final ante']:.2f}"
    return text


# ── The run list and its history ──


_EVALS: dict[tuple[str, int], dict] = {}


def _last_line(path: Path) -> dict:
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 16384))
            lines = f.read().decode("utf-8", "replace").splitlines()
        return json.loads(lines[-1]) if lines else {}
    except (OSError, ValueError):
        return {}


def _eval_summary(path: Path) -> dict | None:
    """An evaluation file's summary; each file is read once (they hold every game)."""
    try:
        key = (str(path), path.stat().st_mtime_ns)
    except OSError:
        return None
    if key not in _EVALS:
        try:
            _EVALS[key] = json.loads(path.read_text(encoding="utf-8"))["summary"]
        except (OSError, ValueError, KeyError):
            return None
    return _EVALS[key]


def last_eval(name: str) -> dict | None:
    evals = sorted(run_dir(name).glob("eval_u*.json"))
    s = _eval_summary(evals[-1]) if evals else None
    return None if s is None else {k: s[k] for k in ("update", "win rate", "win rate +/-", "mean final ante")}


_GAMES: dict[tuple[str, int], list] = {}


def _eval_games(path: Path) -> list | None:
    """An evaluation file's games; each file is read once."""
    try:
        key = (str(path), path.stat().st_mtime_ns)
    except OSError:
        return None
    if key not in _GAMES:
        try:
            _GAMES[key] = json.loads(path.read_text(encoding="utf-8"))["games"]
        except (OSError, ValueError, KeyError):
            return None
    return _GAMES[key]


def eval_vs_start(name: str) -> dict | None:
    """The newest evaluation against update 0's (the policy the run started from) on the
    same seeds, seed by seed; None before a second evaluation."""
    evals = sorted(run_dir(name).glob("eval_u*.json"))
    if len(evals) < 2 or evals[0].name != "eval_u00000.json":
        return None
    games, base = _eval_games(evals[-1]), _eval_games(evals[0])
    c = compare(games, base) if games and base else None
    return None if c is None else {"update": int(evals[-1].stem[6:]), **c}


def last_check(name: str) -> dict | None:
    """The newest final check (``rl.ppo --final-check``): the run against its start."""
    checks = sorted(run_dir(name).glob("check_u*.json"))
    s = _eval_summary(checks[-1]) if checks else None
    return None if s is None or not s.get("vs start") else {"update": s["update"], **s["vs start"]}


def run_overview(name: str, status: dict | None, now: float | None = None) -> dict | None:
    """One row of the run list: the run's status when it has one, else what its files
    say. None for a folder that holds no run."""
    folder = run_dir(name)
    log, state_path = folder / "log.jsonl", folder / "state.json"
    if status is None and not log.exists() and not state_path.exists():
        return None
    state = run_state(name) or {}
    args = state.get("args", {})
    line = _last_line(log) if log.exists() else {}
    activity = max((p.stat().st_mtime for p in (log, state_path) if p.exists()), default=0.0)
    row = {
        "run": name, "copy": args.get("start") or None, "beta": state.get("beta"),
        **(run_settings(args) if args else {}),
        "update": state.get("updates", line.get("update", 0)),
        "hours": state.get("seconds", 0.0) / 3600, "hours total": args.get("hours"),
        "games": state.get("games", line.get("games", 0)),
        "points per game": line.get("points per finished game"),
        "agreement": line.get("agreement with copy"), "drift": line.get("drift"),
        "eval": last_eval(name), "check": last_check(name), "updated": activity,
    }
    if status is not None:
        row.update({k: v for k, v in status.items() if v is not None})
        row["updated"] = max(activity, status.get("updated", 0))
        row["phase"] = status["phase"]
        row["vs start"] = eval_vs_start(name)
        return row
    row["vs start"] = eval_vs_start(name)
    now = time.time() if now is None else now
    if args and state.get("seconds", 0) >= args.get("hours", 0) * 3600:
        row["phase"] = "finished"
    elif now - activity < OUTSIDE_SECS:
        row["phase"] = "outside"
    else:
        row["phase"] = "stopped"
    return row


def list_runs(statuses: dict[str, dict], now: float | None = None) -> list[dict]:
    """Every run, the ones playing first, then the most recently active."""
    folder = RL_DATA / "ppo"
    names = {p.name for p in folder.glob("*") if p.is_dir()} | set(statuses)
    rows = [r for r in (run_overview(n, statuses.get(n), now) for n in names) if r is not None]
    return sorted(rows, key=lambda r: (r["phase"] not in TRAIN_ACTIVE, -r["updated"]))


class History:
    """A run's numbers by update and by evaluation, read again only when its files grow."""

    def __init__(self) -> None:
        self._key: dict[str, tuple] = {}
        self._data: dict[str, dict] = {}

    def of(self, name: str) -> dict:
        folder = run_dir(name)
        log = folder / "log.jsonl"
        evals = sorted(folder.glob("eval_u*.json"))
        key = (log.stat().st_size if log.exists() else -1, tuple(p.name for p in evals))
        if self._key.get(name) != key:
            self._data[name] = self._read(log, evals)
            self._key[name] = key
        return self._data[name]

    @staticmethod
    def _read(log: Path, evals: list[Path]) -> dict:
        data = {"update": [], "points": [], "agreement": [], "drift": [], "evals": []}
        try:
            lines = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
        except OSError:
            lines = []
        for text in lines:
            try:
                line = json.loads(text)
            except ValueError:
                continue
            data["update"].append(line.get("update"))
            data["points"].append(line.get("points per finished game"))
            data["agreement"].append(line.get("agreement with copy"))
            data["drift"].append(line.get("drift"))
        for path in evals:
            s = _eval_summary(path)
            if s is not None:
                data["evals"].append((s["update"], s["win rate"], s["win rate +/-"], s["mean final ante"]))
        return data


# ── The stream ──


def read_stream() -> tuple[dict | None, dict | None]:
    """The launcher's status (``Job.status``) and the player's game and totals."""
    job = jobs.stream()
    return job.status(), read_json(job.file("now.json"))


def model_sources() -> list[str]:
    """What the stream can play: ``run <name>`` for each run with a save, newest first,
    then ``copy <name>`` for each copy."""
    runs = sorted((p for p in (RL_DATA / "ppo").glob("*") if (p / "latest.pt").exists()),
                  key=lambda p: -(p / "latest.pt").stat().st_mtime)
    return [f"run {p.name}" for p in runs] + [f"copy {c}" for c in copies()]


def checkpoints(source: str) -> list[str]:
    """A run's saves: ``latest`` first (followed: read again before each game), then its
    update saves newest first. A copy has one."""
    kind, name = source.split(" ", 1)
    if kind == "copy":
        return [name]
    return ["latest"] + sorted((p.stem for p in run_dir(name).glob("u*.pt")), reverse=True)


def model_choice(source: str, checkpoint: str) -> tuple[Path, bool]:
    """The model file for a choice, and whether the stream follows it."""
    kind, name = source.split(" ", 1)
    if kind == "copy":
        return RL_DATA / "models" / f"{name}.pt", False
    return run_dir(name) / f"{checkpoint}.pt", checkpoint == "latest"


def choice_of(model: str) -> tuple[str, str]:
    """The (model, save) choice a model file stands for: the inverse of model_choice."""
    path = Path(model)
    if path.parent.name == "models":
        return f"copy {path.stem}", path.stem
    return f"run {path.parent.name}", path.stem


def stream_command(settings: dict) -> list[str]:
    path, follow = model_choice(settings["stream_source"], settings["stream_checkpoint"])
    cmd = [venv_python("bot"), "-m", "balatro_bot.stream", "--model", str(path), "--deck", settings["stream_deck"],
           "--stake", settings["stream_stake"], "--delay", str(settings["stream_delay"])]
    return cmd + (["--follow"] if follow else [])
