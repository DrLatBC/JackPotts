# Instance report

Status: done (2026-11-18), committed on `claude/elastic-margulis-18de57`, not merged. Architecture review item #5. `instance_report.py` with `tests/test_instance_report.py` (17 cases) and `tests/test_supervisor_report.py` (14 cases: counts, recent games, explore rate, relaunch, stuck and stall restarts, stream mode, pause and resume, local numbering, the drift case); 6 log-fed tests in `test_gui.py` moved there, the two `progress.txt` tests write a report. Full `pytest -q`: 1034 passed. The 23 reference seeds on Jackdaw: actions identical (4,035) and log lines identical (16,392) against master; game log wording is unchanged. A 3-game `cli` run on Jackdaw with `--batch-number 999` wrote `report_999.jsonl` (3 starts, 3 finishes, 1 win, matching the `Game over` lines) beside `game_999.log`, `scoring_999.log`, `replay_999.jsonl.gz` and `wins_13791_999.log`. Not checked: a supervisor batch end to end (only if Tyler asks). Skill edits are in the side-session notes (`item5-instance-report.md`).
Choices made while building: `batch_numbers` counts game logs in every folder under `bot_log` (as the bot and `compute_session_number` did), so `_rotate_logs` and `stats/finder` now see `stream/` numbers too. A heartbeat goes out only for a new move number, so a stall is measured from the last new number, within 5 s. A slot with every game played relaunches with `--games 0` instead of all its games again.

## Problem

The bot writes human-readable files that the supervisor also uses as data:

| What the supervisor needs | Where it comes from today |
|---|---|
| Win, seed and ante of each finished game (the panel's recent stats and recent wins) | regex on `Game over: VICTORY/DEFEAT … seed= ante=` (`supervisor.py:151`, `Slot._track_game` `:270`) |
| Explore rate of the game in progress | regex on `Explore rate: x` (`:152`), logged by `cli.apply_explore_file` (`cli.py:21`) |
| Stall (no new move for `stall_timeout`) | the `[#n]` counter of `log_action` (`bot_logging.py:179`; `supervisor.py:150`, `:321`) |
| Cycle (the bot repeating itself) | game log lines with the time prefix and `[#n]` stripped, period 1-4 with 5 repeats, held 60 s (`:158`, `:303-349`) |
| Games done, for status and for the next relaunch's `--games` | `progress.txt` `done/total`, written by `cli.py:111,131`, read by `Slot.read_progress` (`:248`) and `_remaining_games` (`:555`) |
| Wins | lines of `wins_<port>_<n>.log` that start with `VICTORY` (`Slot.read_wins` `:294`; written by `bot.WinCaptureHandler`) |

Rewording any of those lines silently breaks soft-lock detection, the counts or the recent stats. None of it has tests.

The batch number is worked out in five places: `cli.py:69-84`, `supervisor.compute_session_number` (`:356`), `_rotate_logs` (`:988`), `stats/finder.find_latest_batch`, and the dashboard (`post_batch_start`). The supervisor hands it to the bot through `next_num.txt`.

**Reproduced drift.** `cli.py:84` names its files after the higher of `next_num.txt` and the highest `game_NNN.log` already in its port folder, while the supervisor watches `game_<batch>.log` (`:602`). In a scratch `bot_log` with `next_num.txt` = 7 and an old `19999/game_042.log`, the bot wrote `game_042.log`, `scoring_042.log` and `wins_19999_042.log`. The supervisor would open `game_007.log`, fail, and `poll_log_for_softlock` returns False for the rest of the batch (`:314`): no soft-lock detection, wins read from `wins_<port>_007` (0), no recent stats or explore rate, and `stats 007` misses the port. It happens whenever a port folder holds a log numbered above the new batch (an offline batch numbered from local logs, then a dashboard batch with a lower number). Today's `bot_log` is consistent (ports at 186-187, `next_num` 187).

Out of scope: `stats/` (the post-batch report) parses about 30 game-log lines (`stats/constants.py`: game over, summary, rounds, plays, buys, sells, packs…). Those lines stay tied to it; moving `stats/` onto structured data is its own follow-up.

## Decisions

1. Glossary term **Instance report** (added): the record an instance's bot keeps of what it is doing, which the supervisor reads to watch the instance.
2. `src/balatro_bot/instance_report.py`, standard library only. Interface:
   - `batch_files(port, number, root=LOG_DIR)`: every per-batch file of a port (`game_NNN.log`, `scoring_NNN.log`, `wins/wins_<port>_NNN.log`, `replay_NNN.jsonl.gz`, `report_NNN.jsonl`), and the `stream/` variant for `--stream-log`.
   - `next_local_number(root=LOG_DIR)`: the one "highest local batch number plus one" rule, for a bot run by hand, the offline supervisor, `_rotate_logs` and `stats/finder`.
   - `Reporter(path, clock=time.time)`, the bot's side: `game_started(explore)`, `game_finished(won, seed, ante)`, `action(seq, state, method, params)`. `action` writes a `heartbeat {seq}` at most every 5 s, and holds the cycle rule over the last 40 moves as data (state, method, params; period 1-4, at least 5 repeats). Once a cycle has lasted 60 s it writes `stuck {reason}`, once per cycle.
   - `Watcher(path, clock=time.time)`, the supervisor's side: `read()` returns the typed events written since the last read and keeps the running totals: games done, wins, explore rate in use, time of the last event, stuck (and why), and the recent finished games.
   - One JSON object per line with `t` (time) and `event`. Each event is flushed when written. The Watcher reads from a byte offset and leaves a half-written last line for the next read.
3. One file per instance per batch, `bot_log/<port>/report_NNN.jsonl`, appended across relaunches within the batch like the game log.
4. Soft-lock: cycles are judged in the bot (decision 2), stalls by the supervisor: no event newer than `stall_timeout` once the bot has written one. A loop of repeated warnings with no moves is caught by the stall timeout (60 s Jackdaw, 600 s live) instead of the cycle rule. A stuck bot keeps playing; the supervisor restarts the whole instance, game server included, as now.
5. The supervisor passes `--batch-number N` to the bot, which uses it as given. Without the flag (a bot run by hand) the bot takes `next_local_number()`. `next_num.txt` and the higher-of rule go. New code says batch number; `session_num` is renamed only where this change touches it, and the pause-state key stays `session_num`.
6. `progress.txt` goes: games done come from the Watcher, the total is the supervisor's own `games`. The supervisor stops reading the wins file; the bot keeps writing it as the full log of each win. `wins.txt` for OBS in `--stream` mode stays.
7. On `--resume` and on every relaunch, a slot's games done come from reading its report from the start. New pause states drop `recent` and `recent_wins` (the report rebuilds them). A batch paused before this lands has no report file and can't be resumed; the supervisor says so. No batch is paused now.
8. Game log wording is unchanged: `Game over` and `Summary` stay for `stats/`, `Explore rate:` and `[#n]` stay for people reading the log. The supervisor reads no game log line after this. `_rotate_logs` archives `report_NNN.jsonl` with the game logs.

## Steps

1. **Report module.** `instance_report.py` and `tests/test_instance_report.py`: round trip of every event through a Reporter and a Watcher; a half-written line read on the next `read()`; reading on after a relaunch appends; totals (games, wins, explore rate, recent); heartbeat throttling and the cycle rule with an injected clock (period 1 to 4 flagged after 60 s, a broken cycle resets, period 5 not flagged, `stuck` written once); `batch_files` names and `next_local_number` over a scratch folder. Proof: the new tests.
2. **Bot side.** `cli.py` takes `--batch-number`, names its files through `batch_files`, writes no `progress.txt`, and gives `run_bot` a Reporter (`game_started` with the explore rate before each game, `action` beside `recorder.sending`, `game_finished` after `recorder.finish`). Proof: targeted tests; the 23 reference seeds on Jackdaw (`scripts/trace/record.py`, `BALATRO_KERNEL=rust`, `scripts/trace/compare.py` against a master trace) replay with identical actions and log lines.
3. **Supervisor side.** Each Slot holds a Watcher on its report. `check_slot` restarts on `stuck` or a stall, `launch_bot` takes games remaining from the Watcher and passes `--batch-number`, `_status` reads counts, wins, explore rate and recent games from it. Removed: the three regexes, `_has_repeating_cycle`, `poll_log_for_softlock`, `read_progress`, `read_wins`, `_remaining_games`' file read, `next_num.txt`, the `progress.txt` cleanup, `compute_session_number` (now `next_local_number`). `_rotate_logs` and `stats/finder` use the module's naming. Resume follows decision 7. New tests in `tests/test_supervisor_report.py`, without processes: a stuck event restarts the slot, a stall restarts it after `stall_timeout` and not before, no event yet means no stall, games done and remaining across a relaunch, wins and recent stats from the report, resume rebuilds counts and refuses without a report. The two `progress.txt` tests in `test_gui.py` write a report instead. Proof: targeted tests, full `pytest -q`, the reference seeds as in step 2.
4. **Docs.** `docs/architecture.md`, this plan's status and results, and the batch skill's lines on `progress.txt`, `next_num.txt` and soft-lock detection (sent to the parent session as old → new text, since gitignored skill files can't be edited from a worktree). A tiny sim batch end to end only if Tyler asks, launched outside the Claude app.
