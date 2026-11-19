"""The instance report: what the bot writes, the supervisor reads back."""

from __future__ import annotations

from balatro_bot import instance_report as ir
from balatro_bot.instance_report import (
    GameFinished, GameStarted, Heartbeat, Reporter, Stuck, Watcher, batch_files, next_local_number,
)


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _report(tmp_path, clock=None):
    path = tmp_path / "12346" / "report_007.jsonl"
    return path, Reporter(path, clock or Clock()), Watcher(path)


def test_every_event_round_trips(tmp_path):
    clock = Clock()
    path, rep, watch = _report(tmp_path, clock)
    rep.game_started(0.15)
    rep.action(0, "BLIND_SELECT", "select", None)
    rep.game_finished(True, "ABC123", 9)
    clock.t += ir.CYCLE_HOLD + 1
    rep._write(Stuck(clock.t, "why"))
    assert watch.read() == [
        GameStarted(1000.0, 0.15), Heartbeat(1000.0, 0), GameFinished(1000.0, True, "ABC123", 9),
        Stuck(1061.0, "why"),
    ]
    assert (watch.games, watch.wins, watch.explore, watch.stuck) == (1, 1, 0.15, "why")


def test_read_returns_only_new_events(tmp_path):
    _, rep, watch = _report(tmp_path)
    rep.game_started(0.0)
    assert len(watch.read()) == 1
    assert watch.read() == []
    rep.game_finished(False, "S", 3)
    assert watch.read() == [GameFinished(1000.0, False, "S", 3)]


def test_missing_report_reads_empty(tmp_path):
    assert Watcher(tmp_path / "nothing.jsonl").read() == []


def test_half_written_line_waits_for_next_read(tmp_path):
    path, rep, watch = _report(tmp_path)
    rep.game_started(0.0)
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"event": "game_finished", "t": 1.0, "won": fa')
    assert [type(e) for e in watch.read()] == [GameStarted]
    with path.open("a", encoding="utf-8") as fh:
        fh.write('lse, "seed": "S", "ante": 4}\n')
    assert watch.read() == [GameFinished(1.0, False, "S", 4)]


def test_unknown_or_broken_lines_are_skipped(tmp_path):
    path, rep, watch = _report(tmp_path)
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"event": "later_kind", "t": 1}\nnot json\n')
    rep.game_finished(True, "S", 9)
    assert watch.read() == [GameFinished(1000.0, True, "S", 9)]


def test_relaunched_bot_appends_and_totals_carry_on(tmp_path):
    path, rep, watch = _report(tmp_path)
    rep.game_finished(False, "A", 2)
    rep.close()
    watch.read()
    watch.bot_started()
    rep2 = Reporter(path, Clock())
    rep2.game_started(0.0)
    rep2.game_finished(True, "B", 9)
    watch.read()
    assert (watch.games, watch.wins) == (2, 1)
    # A fresh watcher on the same file (a resumed supervisor) counts the same.
    again = Watcher(path)
    again.read()
    assert (again.games, again.wins) == (2, 1)


def test_heartbeats_throttled_and_only_for_a_new_seq(tmp_path):
    clock = Clock()
    _, rep, watch = _report(tmp_path, clock)
    rep.action(0, "SHOP", "buy", {"card": 0})
    clock.t += 1
    rep.action(1, "SHOP", "buy", {"card": 1})       # inside 5 s: not written
    clock.t += ir.HEARTBEAT_EVERY
    rep.action(1, "SHOP", "buy", {"card": 1})       # seq 1 not sent yet: written
    clock.t += ir.HEARTBEAT_EVERY
    rep.action(1, "SHOP", "buy", {"card": 1})       # same seq: not written
    assert watch.read() == [Heartbeat(1000.0, 0), Heartbeat(1006.0, 1)]


def test_stall_measured_from_the_last_new_seq(tmp_path):
    clock = Clock()
    _, rep, watch = _report(tmp_path, clock)
    assert not watch.stalled(clock.t + 10_000, 60)   # no move yet: no stall
    rep.action(3, "SHOP", "reroll", None)
    watch.read()
    assert not watch.stalled(clock.t + 59, 60)
    assert watch.stalled(clock.t + 60, 60)
    watch.bot_started()
    assert not watch.stalled(clock.t + 10_000, 60)


def test_game_start_counts_as_a_move_and_resets_seq(tmp_path):
    clock = Clock()
    path, rep, watch = _report(tmp_path, clock)
    rep.action(5, "SHOP", "reroll", None)
    clock.t += 100
    rep.game_started(0.0)
    clock.t += ir.HEARTBEAT_EVERY
    rep.action(5, "BLIND_SELECT", "select", None)  # same number, new game: a move
    watch.read()
    assert watch.last_move == clock.t


def _cycle(rep, clock, period, moves, step=1.0):
    pattern = [("SHOP", "buy", {"card": i}) for i in range(period)]
    for i in range(moves):
        state, method, params = pattern[i % period]
        rep.action(7, state, method, params)
        clock.t += step


def test_cycles_of_period_one_to_four_report_stuck_after_the_hold(tmp_path):
    for period in (1, 2, 3, 4):
        for moves, stuck in ((40, False), (85, True)):  # cycle seen by move 20, 1 s a move
            clock = Clock()
            path = tmp_path / f"p{period}_{moves}.jsonl"
            rep, watch = Reporter(path, clock), Watcher(path)
            _cycle(rep, clock, period, moves)
            watch.read()
            assert (watch.stuck is not None) == stuck, (period, moves)


def test_stuck_written_once_per_cycle(tmp_path):
    clock = Clock()
    path, rep, watch = _report(tmp_path, clock)
    _cycle(rep, clock, 2, 200)
    assert sum(isinstance(e, Stuck) for e in watch.read()) == 1


def test_period_five_is_not_a_cycle(tmp_path):
    clock = Clock()
    path, rep, watch = _report(tmp_path, clock)
    _cycle(rep, clock, 5, 200)
    watch.read()
    assert watch.stuck is None


def test_a_broken_cycle_starts_the_hold_again(tmp_path):
    clock = Clock()
    path, rep, watch = _report(tmp_path, clock)
    _cycle(rep, clock, 1, 50)                          # 30 s into the cycle
    rep.action(7, "SHOP", "sell", {"joker": 0})        # breaks it
    clock.t += 1
    _cycle(rep, clock, 1, 50)                          # new cycle, only ~30 s
    watch.read()
    assert watch.stuck is None


def test_new_game_clears_the_cycle(tmp_path):
    clock = Clock()
    path, rep, watch = _report(tmp_path, clock)
    _cycle(rep, clock, 1, 50)
    rep.game_started(0.0)
    _cycle(rep, clock, 1, 50)
    watch.read()
    assert watch.stuck is None


def test_has_repeating_cycle():
    assert ir.has_repeating_cycle(["a"] * 5)
    assert not ir.has_repeating_cycle(["a"] * 4)
    assert ir.has_repeating_cycle(["x", *["a", "b"] * 5])
    assert not ir.has_repeating_cycle(list("abcdeabcdeabcdeabcdeabcde"))


def test_batch_files(tmp_path):
    f = batch_files(12346, 7, tmp_path)
    assert f.game_log == tmp_path / "12346" / "game_007.log"
    assert f.scoring_log == tmp_path / "12346" / "scoring_007.log"
    assert f.wins_log == tmp_path / "wins" / "wins_12346_007.log"
    assert f.replay == tmp_path / "12346" / "replay_007.jsonl.gz"
    assert f.report == tmp_path / "12346" / "report_007.jsonl"
    s = batch_files(12346, 7, tmp_path, stream=True)
    assert s.game_log == tmp_path / "stream" / "game_007.log"
    assert s.wins_log == tmp_path / "wins" / "wins_12346_007.log"


def test_next_local_number(tmp_path):
    assert next_local_number(tmp_path) == 1
    for port, n in ((12346, 41), (12347, 186), (12347, 9)):
        (tmp_path / str(port)).mkdir(exist_ok=True)
        (tmp_path / str(port) / f"game_{n:03d}.log").touch()
    (tmp_path / "12346" / "scoring_500.log").touch()
    assert next_local_number(tmp_path) == 187
