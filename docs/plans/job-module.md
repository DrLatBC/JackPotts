# Job module

Status: done (2026-11-18), committed in 1ac1b84. `jobs.py` plus `tests/test_jobs.py` (22 cases); the batch, learning runs and the stream moved onto it; `files.py` deleted; `test_gui.py` down from 36 monkeypatches to 28 (the rest aren't the job protocol). Targeted tests: 160 in `.venv`, 53 in `.venv-rl`. Live check from a panel run on this branch against its own `bot_log`, launched outside the Claude app: a 2-instance sim batch started, finished (`finished`), stopped, paused, resumed (same session and counts), paused, and ended with finish-paused; a learning run started, stopped and was killed; the stream started, stopped and was killed. Each killed job read as failed on its tab, with the dead launcher's game closed. Found live and fixed: a claim is only the shared fields, so the Batches tab reads a claimed batch with no snapshot yet as starting (a `KeyError` there had stopped the panel's refresh); the supervisor claims after its argument checks, so a refused command line never writes a status; the stream player resolves its default folder before using it; `failed` shows ahead of "outside", and Start stays off while instance ports are held. Step 5 done: docs and the batch skill updated (the main checkout's gitignored `.claude/skills/batch/` included), and the real `bot_log` moved to the job layout by a one-off script; every old status reads back through `jobs` with its phase.

## Problem

The control panel starts, watches and stops three jobs (see GLOSSARY.md): the batch (`supervisor`), learning runs (`rl.ppo train`) and the stream (`balatro_bot.stream`, with the `rl.stream` player as its child). Each job has its own version of the same file protocol:

| | Batch | Learning run | Stream |
|---|---|---|---|
| Files | `bot_log/supervisor_status.json`, `supervisor.stop`, `supervisor.pause`, `supervisor_commands/`, `supervisor_console.log` | `bot_log/training/<run>.json`, `<run>.stop`, `<run>.log` | `bot_log/model_stream/status.json`, `stop`, `launcher.log` |
| Phases written | starting, running, stopping, pausing, paused, stopped | running, stopped, finished, failed | starting, running, stopping, stopped, failed |
| Status write | hand-rolled `os.replace`, no retry (`supervisor._write_status`) | `ppo.write_json` + `files.replace_file` | hand-rolled + `files.replace_file` |
| Live phase, dead pid | `gui.read_status`: reads as no batch | `learning.read_trainings`: failed; `ppo.playing_elsewhere` | `learning.read_stream`: failed |
| `pid_alive` | `learning.pid_alive` (Windows only) | `ppo.pid_alive` (Windows + POSIX) | `learning.pid_alive` |
| Stop request | deleted when seen | deleted when seen | left in place (launcher and player both read it) |
| Second copy | panel's port check only | `claim()` refuses | nothing |
| Spawn | `gui._spawn_supervisor` | `learning.launch_training` | `learning.launch_stream` |

Atomic writes are hand-rolled three times, including `gui.send_command` and `gui.set_explore_rate`. `tests/test_gui.py` monkeypatches both modules' path constants (36 patches). A batch whose supervisor died shows as idle while its dashboard batch may still read running.

## Decisions

1. `src/balatro_bot/jobs.py` owns a job's folder, status, phases, liveness, requests, commands and spawn. It uses only the standard library, so `.venv-rl` and PyPy import it from `src` (ADR 0010), as they import `files.replace_file` today. It stays file-based because the jobs are separate processes that outlive the panel. `files.py` is folded in and deleted, and `learning.read_json` (last good copy when a read lands on a write) moves in as the one reader.
2. One layout. Each job is a folder holding `status.json`, `stop`, `pause`, `commands/` and `console.log`: `bot_log/batch/`, `bot_log/training/<run>/`, `bot_log/model_stream/`. Job-specific files sit beside them (`now.json`, `games.jsonl`, `game.log`, `stream.log`, `divergences/`) through `job.file(name)`. Callers name a job, never a path.
3. One phase vocabulary: `starting, running, stopping, pausing, paused, stopped, finished, failed`. `starting`, `running`, `stopping` and `pausing` count as live, and each job writes the subset it uses. A batch that plays all its games writes `finished`. Training's `doing` (starting / playing / evaluating / checking) stays as the detail under `running`.
4. Status envelope, written by the module: `job`, `phase`, `pid`, `updated`, `started`, `doing`, `error`, `note`. The rest is the job's own and opaque to the module (the batch's slots, totals and `resume`; a learning run's update, eval and check; the stream's model, deck and ports). `job.report(status, **changes)` merges the changes, stamps `pid` and `updated`, and swaps the file in with the Windows retry. A write that doesn't get through is logged and skipped. It never ends a job.
5. `job.status()` is the one read. It returns None with no status file. A live phase whose pid is dead reads as `failed` ("The process ended without saving its status."), so a crashed batch now shows red instead of idle. `running` with a stop asked reads as `stopping`. One cross-platform `pid_alive` (from `ppo`'s copy). The panel, `learning_sync` and the claim guard all read through it.
6. `job.claim()` refuses when the status is live under another living pid, clears stale requests and writes `starting`. All three jobs claim, so a second batch or stream launcher refuses as a second `train` does today. The panel's port check stays as a guard against a batch on older code started from a terminal.
7. Requests: `job.ask("stop" | "pause")`, `job.asked(...)`. These are flags the job checks without using them up, cleared by `claim()` and on exit, so both stream processes see a stop. Commands are a generic queue: `job.send(cmd)` writes one JSON file, and `job.take()` returns and removes them, oldest first (the batch's add/remove).
8. `job.spawn(cmd, append=False) -> Popen` is the one launcher. It clears the requests, opens `console.log`, sets cwd to the repo, and is windowless in its own process group on Windows (plain elsewhere). `job.console_tail()` gives the last printed line for a launch that exits before its first status and for hover text.
9. Lookup: `jobs.batch()`, `jobs.learning_run(name)`, `jobs.learning_runs()`, `jobs.stream()` read `jobs.ROOT` (default `bot_log/`) at call time. Tests set `ROOT` to `tmp_path` in one fixture and use real files, which is the test adapter. `ppo.TRAINING` and `rl.stream.STREAM` go away. The player keeps its `out` folder argument (the overlay's `SimBackend` test), defaulting to `jobs.stream().folder`.
10. `learning.py` keeps only what isn't the job protocol: run names, run list and history, evaluations, model choices and commands. The "outside" reading for runs with no status file stays there.
11. Cutover: a one-off script, run from the scratchpad and not committed. It refuses while any job is live, then moves `training/<run>.json|.log` → `training/<run>/status.json|console.log`, `supervisor_status.json|supervisor_console.log` → `batch/status.json|console.log` (a paused batch keeps its `resume` block, so `--resume` works), and `model_stream/launcher.log` → `console.log`. No code reads the old paths.

## Steps

1. `jobs.py` and `tests/test_jobs.py` against a temp root: report/status round trip, dead pid → failed, stop asked → stopping, claim refusal and claim over a dead pid, requests seen by two readers, the command queue in order, the retry on a held file, and spawn running a tiny real command into `console.log`.
2. The batch on `jobs`: `supervisor` (claim, report, requests, `take()`, `finished`) and `gui`'s batch side (status, ask, send, spawn, the `failed` colour). `tests/test_gui.py` and `tests/test_supervisor_job.py` move to the root fixture.
3. Learning runs on `jobs`: `rl/ppo.py`, `learning.py`, `learning_sync.py`, `TrainTab`. `tests/test_learning.py` and `tests/test_learning_sync.py`.
4. The stream on `jobs`: `balatro_bot.stream`, `rl.stream`, `StreamTab`, `overlay`'s default folder. Delete `files.py`. `tests/test_stream_launcher.py` and `rl/tests/test_stream.py`.
5. The batch skill, `docs/usage.md` and module docstrings get the new paths. Run the cutover script with the panel closed.
6. Live check from the panel, launched outside the Claude app: start, stop and pause/resume a short simulator batch, start and stop a throwaway learning run, start and stop the stream. Then check a crashed job (kill its pid) reads as failed on each tab. Run the targeted tests in both venvs.
