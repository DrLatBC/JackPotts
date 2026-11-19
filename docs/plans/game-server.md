# Game server

Status: done (2026-11-18), uncommitted. Architecture review item #7. Targeted tests 172 in `.venv` and 28 in `.venv-rl`, full suite 864; the 23 reference seeds on Jackdaw replayed with identical actions and log lines against the old code (4,035 entries, 16,393 lines). Live, from this branch against its own `bot_log`, launched outside the Claude app: a 2-instance simulator batch ran its 6 games to `finished` with bots on PyPy and Jackdaw served by the worktree's adapter with `-I`; a 1-instance live batch started Balatro headless and fast in the port's own profile, played its game and shut down cleanly; the stream opened the visible game on the unlocked profile, the player moved, and a stop request ended it with the game closed and the port free. Not checked live: the `+jackdaw` dashboard tag, since the worktree has no `.env.local`; `Supervisor._code_version` returns it on Jackdaw. `game_server.py` holds both adapters, the profile and unlock code and `read_health`; the supervisor, the stream and the bot use it; config owns every path and `venv_python`; `tie_children` and `kill_port_processes` are in `jobs.py`. `tests/test_game_server.py` (14 cases) replaces the PyPy tests in `test_gui.py` and the profile tests in `test_stream_launcher.py`.
Deviation from decision 2: `start(port, creationflags)` takes the window and priority flags from its caller. The supervisor gives its bots the same flags (hidden and below normal for a batch, a console each with `--stream`), so they stay a batch setting, not a server one.

## Problem

The two game servers (see GLOSSARY.md), the live game and Jackdaw, have no module of their own. The supervisor tells them apart by whether it was handed a `sim_python`, and the other jobs copy or borrow its pieces.

| What differs | Where it is decided today |
|---|---|
| Launch command, env, window flags | `Supervisor.launch_server` (`supervisor.py:744`): Jackdaw is `[sim_python, -I, serve.py, --port]`; live is `balatrobot serve` with the isolated `APPDATA`, headless and fast unless `--stream` |
| Live launch, again | `stream.run` (`stream.py:147`): its own `balatrobot serve` command, windowed, output to `game.log`, unlocked profile |
| Profile | `supervisor.ensure_isolated_profile` (copied); `stream.unlock_all` and `game_items` (unlocked) |
| Bot interpreter | `Supervisor.__init__` (`supervisor.py:585`): PyPy on Jackdaw when `.venv-pypy` exists and the kernel is off, else CPython (ADR 0010) |
| Stall timeout | `supervisor.py:582`: 600 s live, 60 s on Jackdaw |
| Version tag | `Supervisor._code_version` (`supervisor.py:740`): `+jackdaw` |
| Startup runtime line | `Supervisor.run` (`supervisor.py:1101-1113`) |
| Settled responses | the server's `health` reply (`serve.py:41` adds `simulator`), parsed inline in `bot.wait_for_server` (`bot.py:115`) |
| Stopping the server | `supervisor.graceful_kill`, used for Jackdaw too; the stream imports it |
| Health | `Slot.check_health` (TCP connect) and `stream.listening` (the same check, written twice) |
| Jackdaw interpreter exists | `supervisor.main` (`supervisor.py:1336`) |

Interpreter paths are worked out in five places: `supervisor.py:44-54` (`.venv`, `.venv-jackdaw`, `.venv-pypy`), `learning.py:20` and `stream.py:38` (`.venv-rl`), and the repo root again in `jobs.py:26`. `SupervisorConfig.python_path` is read by the supervisor only and set by nothing. The stream and learning import `BASE_DIR`, `PYTHON`, `cfg`, `ensure_isolated_profile`, `graceful_kill`, `kill_port_processes` and `tie_children_to_supervisor` from the supervisor, and gui imports `PYTHON` and `LOG_DIR` from it.

## Facts

- Whether responses arrive settled is reported by the server itself: the Jackdaw adapter's `health` reply carries `simulator` and `simulator_version`, and the live game's carries neither. A bot pointed at a port by hand, with no supervisor, reads it the same way.
- Jackdaw runs in-process in rl self-play and in the stream's shadow. Neither starts a game server, so neither is part of this.
- `kill_port_processes` and the kill-on-exit job object are used by both the supervisor and the stream, and are about processes rather than batches.

## Decisions

1. Glossary term **Game server** (added): what a bot plays against on a port, the live game or the simulator. **Instance** is now one game server plus the bot driving it.
2. `src/balatro_bot/game_server.py`, standard library only so the bot imports it on PyPy 3.12. Two adapters, `LiveGame` and `Jackdaw`, frozen, each with:
   - `start(port) -> Popen`: launch command, profile, env and window flags
   - `health(port) -> bool`: the TCP probe
   - `stop(proc)`: graceful for the live game, so Balatro finishes its saves; a plain kill for Jackdaw
   - `bot_python(kernel) -> str`
   - `stall_timeout`: 600 s live, 60 s Jackdaw
   - `version_tag`: `""` or `"+jackdaw"`
   - `name`: `"live"` / `"jackdaw"`, for the startup line

   Nothing else is on the interface. Restarts, cooldown and softlock detection stay in the supervisor: they are batch logic.
3. Settled stays in the server's `health` reply. `game_server` owns the one parser for that reply (settled, simulator name and version), and `bot.wait_for_server` uses it.
4. `Jackdaw.bot_python(kernel)` keeps ADR 0010's rule unchanged: PyPy when `.venv-pypy` (or `$BOT_PYPY`) exists and the kernel is off, else CPython. `LiveGame.bot_python` is always CPython. The kernel stays a batch setting: the supervisor still sets `BALATRO_KERNEL` for its bots and checks the kernel is built. The adapter only takes it as input to choosing the interpreter.
5. `LiveGame(visible: bool, profile: "copied" | "unlocked", log: Path | None)` covers the three uses: batch (hidden, headless, fast, copied), supervisor `--stream` (visible, own console, copied), and the model stream (visible, `game.log`, unlocked). `ensure_isolated_profile`, `unlock_all` and `game_items` move into `game_server`. The supervisor's `--stream` mode stays as it is.
6. Paths live in `config.py`: the repo root, log dir, profiles dir, and `venv_python(name, windowless=False)` for `.venv` (falling back to the running interpreter in a worktree), `.venv-jackdaw` / `$JACKDAW_PYTHON`, `.venv-pypy` / `$BOT_PYPY` and `.venv-rl`. gui, learning, stream and jobs read them from there, not from the supervisor. `jobs.py` takes its repo root from config (also standard library only, so `.venv-rl` still imports it). `SupervisorConfig.python_path` is dropped.
7. `tie_children_to_supervisor` and `kill_port_processes` move to `jobs.py`, beside `spawn`, as `tie_children()` and `kill_port_processes()`. `graceful_kill` becomes `LiveGame.stop`. The stream then imports nothing from the supervisor.
8. The supervisor holds one `server` in place of `sim_python`, and every `sim_python` branch goes. The pause state keeps its `sim` key, so a batch paused before this lands still resumes. `--sim` and the panel's SIMULATOR box stay and are mapped to an adapter in `main()`, which also reports a missing Jackdaw interpreter as now.

## Steps

1. **Paths and process helpers.** Paths into config (decision 6), `tie_children` and `kill_port_processes` into jobs (decision 7), every caller moved. No behaviour change. Proof: targeted tests; the reference seeds (`scripts/trace/seeds.txt`) on Jackdaw replay with identical actions and log lines.
2. **Game server.** Add `game_server.py` with both adapters and move the profile code in. Move the supervisor onto `server`, the stream onto `LiveGame(visible=True, profile="unlocked", log=…)`, and `bot.wait_for_server` onto the reply parser. Delete the `sim_python` branches. New `tests/test_game_server.py` tests the adapters without building a Supervisor: the interpreter rule (PyPy present, missing, kernel on, live), stall timeouts and version tags, the commands each `start` builds (live batch, visible stream with the unlocked profile, Jackdaw) without starting them, and the reply parser on a Jackdaw and a live reply. It replaces the two PyPy tests in `test_gui.py`. Proof: targeted tests and the reference seeds as in step 1.
3. **Live checks**, launched outside the Claude app through `scripts/start_batch.vbs` and the panel, each after Tyler's OK and with nothing else running: (a) a 2-instance simulator batch of a few games: startup line says PyPy, code version ends `+jackdaw`, reaches finished; (b) a 1-instance live-game batch of 1-2 games: Balatro starts headless, the bot plays, stop is clean; (c) the stream started and stopped from the Stream tab. Then the docs and the batch skill (the main checkout's gitignored copy too) are updated.
