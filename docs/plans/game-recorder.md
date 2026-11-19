# Game recorder and play dispatcher

Status: built (2026-11-18), all four steps. `run_bot` is 111 lines (was 636), `bot.py` 238 (was 957). The 23 reference seeds on the kernel, recorded with and without replays: after each of steps 2, 3 and 4, every action (3,593; 3,616 with replays), all 14,773 log lines and all 23 payloads (and replays) match the baseline. The seeds cover 2 wins and 108 rearranges. Unit tests: 832 pass, including `tests/test_dispatcher.py` (14) and `tests/test_game_recorder.py` (13); rl `test_ordering.py` and `test_stream.py` pass (29).

Built differently from the decisions below:
- The recorder's per-action calls are `sending(game_state, method, params, action, decision, state)` before the call, then `acted(state, recording_at)`, `refused(error)` or `timed_out()`. The action line has to be logged before the dispatcher logs a refusal, so the recorder reads the state it acts on before the call goes out. The decision snapshot is taken after the rearrange; plays never carry a shop or pack decision, so the snapshot is the same.
- After a timeout the next tick's `observe` checks for victory; the timeout path no longer does it as well. Only the order of the log lines changes, and only in a timeout.
- `_boss_disabled` passes to every state the dispatcher returns. Before, it was lost on the gamestate refresh after a rearrange and on the polls while the game passes through HAND_PLAYED and the like, so a rearranged play after Luchador was sold scored the boss as active. The reference seeds never hit that case.
- The wins-file flush moved from `log_game_over` to `run_bot`, which owns the win handler. `find_current_blind` moved to `bot_logging`, its only user.

## Problem

`run_bot` (`bot.py:324-957`, 636 lines) is the decision loop, the RPC sender and the game's bookkeeper at once:

| Concern | Where now |
|---|---|
| Decision loop, polling | `bot.py:384-465` |
| Rearrange-then-play | `bot.py:474-486`; again in `rl/ordering.put_in_order` and `rl/stream.Player.move` |
| Stream highlights and delay | `bot.py:491-498`, `750-751` |
| `_boss_disabled` carried onto each new state | six places in `bot.py` |
| Timeout re-poll, pack-wedge skip, forced skip after 5 errors | `bot.py:752-854` |
| Reaching into rules after an error (`_blocked_consumables`, `evaluator.slots_full`) | `bot.py:794-836` |
| Pre-call label captures and about 12 `gs.*.append` | `bot.py:500-746` |
| `getattr(engine.last_rule, "last_decision")` | `bot.py:468` |
| Death info, replay, dashboard payload (replayable or full, ADR 0011) | `bot.py:855-955` |

The game record is also kept in `bot_logging.py`: `log_blind_result` appends `round_results`, `log_ante_transition` writes `ante_snapshots` and `joker_scaling`, `log_shop_state` appends `shop_events`, `log_blind_transition` sets the blind context, `detect_joker_changes` and `record_decision` write `gs` too. The engine reads the record back each tick (`_compute_live_stats` → `state["_live_stats"]`, `build_decision_state`).

The loop's only tests are the 56 integration tests, each needing a server. Nothing tests the payload.

## Facts from the code

- The rule hands `bot.py` the played cards already in play order. `bot.py` sends one `rearrange` with `hand_order_for_play(order, hand_size)`, then plays `sorted(order)`; if the rearrange fails it plays the original order.
- rl works out the order itself (`bot_order`) and applies it as `SwapHandLeft` steps (`left_moves`), with no fallback. `rl/stream.Player.move` repeats `put_in_order`, stepping the live adapter instead of the game.
- The only logic all three share is `hand_order_for_play` plus `sorted`.
- `explored_actions` stores a position in `client.recording`, so it needs the recording's length after the call.
- `_check_victory`'s `pre_play_chips` argument is unused.
- `scripts/trace/record.py` traces every non-read call and every log line. With no batch id, `run_bot` posts nothing, so payloads are not traced.

## Decisions

1. **One shared pure function, not a shared class.** `play_plan(order, hand_size) -> (hand_order | None, play_indices)` goes in `domain/scoring/play_order.py` beside `hand_order_for_play`. The bot dispatcher, `rl.ordering.put_in_order` and `rl/stream` use it. `stream.move`'s copy is replaced by `put_in_order(game, cards, step=game.step)` with `step=self.adapter.step`.
2. **Dispatcher** (`src/balatro_bot/dispatcher.py`) owns sending one action: rearrange-then-play with its fallback, stream highlights and delay, carrying `_boss_disabled` onto every state it returns, the timeout re-poll, the pack-wedge forced skip (and its abort at 2), and the forced skip after 5 consecutive errors. It returns the new state and an outcome: acted, refused (with the `APIError`), or timed out, plus the recording position for exploration. `run_bot` hands the outcome to the recorder.
3. **Engine feedback.** The two rule pokes move unchanged into `RuleEngine.refused(game_state, method, params, error)` in `engine.py`, called by the dispatcher on a refusal. `RuleEngine.last_decision` replaces the `getattr` on `last_rule`. No rules or policy files are touched (they belong to the joker-value work).
4. **Recorder** (`src/balatro_bot/game_recorder.py`) makes every change to the game record; `GameLoopState` becomes private to it. Interface:
   - `observe(state)`: once per tick before deciding (victory, `won` logging, ante transition, max chips, shop/pack/tag offers, blind and hand context).
   - `acted(before, method, params, action, decision, after, recording_at)`: after a successful call; does the label captures from `before` and every append now in `bot.py:583-746`.
   - `refused(before, method, params, decision, error)`: records the decision with its error and counts the error.
   - `finish(final_state)`: game-over log and death info.
   - Reads: `live_stats()`, `decision_state(state)`, `won`, `explored_actions`.
   - `payload(final_state, *, replayable, port, deck, stake, log_text) -> dict`: the dashboard body, cut down when replayable.
5. **`bot_logging` writes text only.** Its functions take values, not `gs`; the recorder decides when something happened and calls them. `record_decision`, `build_decision_state` and `detect_joker_changes` move into the recorder.
6. **`run_bot` becomes the loop**: start or join, `observe`, decide, dispatch, `acted`/`refused`, then `finish`, write the replay, post `payload`. The win-capture log text stays in `run_bot`.
7. **Tests**: `tests/test_game_recorder.py` drives the recorder with canned state dicts through a short game (blind select, plays, a shop with buy/sell/reroll, a pack pick and skip, a loss) and checks the record and both payload forms. `tests/test_dispatcher.py` uses a fake client: play in order, play needing a rearrange, rearrange failure, refusal, timeout, pack wedge, five errors. `play_plan` gets a table test.
8. **Proof of no behaviour change**: `record.py` gains payload capture (a stand-in `post_game` appends each payload to the trace, with a dummy batch id). The 23 reference seeds on `BALATRO_KERNEL=rust` are recorded twice, with replays (cut-down payload) and without (full payload), on the old commit and after each step; actions, log lines and payloads must be identical. The rl tests (`rl/tests/test_ordering.py`) must pass unchanged.

## Steps

1. Add payload capture to `record.py`; record the baseline traces (with and without replays) on the current commit.
2. `play_plan` with its test; the dispatcher with its test; `RuleEngine.refused` and `last_decision`; switch `run_bot`, `put_in_order` and `stream.move`. Compare traces.
3. The recorder with its test; `run_bot` calls it; `GameLoopState` and the capture helpers leave `bot.py`. Compare traces.
4. `bot_logging` to text only; the record-keeping parts move into the recorder. Compare traces; record results here.
