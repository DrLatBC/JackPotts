"""Main bot loop — connects to balatrobot and runs the decision engine."""

from __future__ import annotations

import logging
import logging.handlers
import time

import httpx
from balatrobot import APIError

from balatro_bot.dispatcher import Dispatcher
from balatro_bot.domain.policy.exploration import explorer
from balatro_bot.domain.scoring.draws import seed_draws
from balatro_bot.engine import RuleEngine
from balatro_bot.game_server import read_health
from balatro_bot.game_recorder import PASSING_STATES, GameRecorder
from balatro_bot.infrastructure.client import BalatroClient
from balatro_bot.instance_report import Reporter
from balatro_bot.replays import ReplayWriter, replay_record

log = logging.getLogger("balatro_bot")
_stream_log = logging.getLogger("balatro_stream")


class WinCaptureHandler(logging.handlers.MemoryHandler):
    """Buffers all log records; on flush_win() writes them to wins.txt."""

    def __init__(self, fmt: logging.Formatter, wins_file: str = "wins.txt") -> None:
        super().__init__(capacity=100_000, flushLevel=logging.CRITICAL + 1, target=None)
        self.fmt = fmt
        self.wins_file = wins_file

    def shouldFlush(self, record: logging.LogRecord) -> bool:
        return False

    def get_log_text(self) -> str:
        """Return buffered log records as a single string."""
        return "\n".join(self.fmt.format(record) for record in self.buffer)

    def flush_win(self, seed: str) -> None:
        with open(self.wins_file, "a", encoding="utf-8") as f:
            f.write(f"\n{'='*60}\n")
            f.write(f"VICTORY — seed {seed}\n")
            f.write(f"{'='*60}\n")
            for record in self.buffer:
                f.write(self.fmt.format(record) + "\n")
        # Don't clear buffer here — bot.py captures log_text after this

    def reset(self) -> None:
        self.buffer.clear()


_win_handler: WinCaptureHandler | None = None


def setup_logging(
    verbose: bool = False,
    log_file: str = "growing.txt",
    wins_file: str = "wins.txt",
    scoring_file: str | None = None,
    stream_file: str | None = None,
) -> None:
    global _win_handler
    level = logging.DEBUG if verbose else logging.INFO
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(fmt)

    file_handler = logging.FileHandler(log_file, mode="a", encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(fmt)

    _win_handler = WinCaptureHandler(fmt, wins_file=wins_file)
    _win_handler.setLevel(logging.INFO)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    if not stream_file:
        root.addHandler(console)
    root.addHandler(file_handler)
    root.addHandler(_win_handler)

    logging.getLogger("httpx").setLevel(logging.WARNING)

    if scoring_file:
        scoring_log = logging.getLogger("balatro_scoring")
        scoring_log.setLevel(logging.INFO)
        scoring_log.propagate = False
        sh = logging.FileHandler(scoring_file, mode="a", encoding="utf-8")
        sh.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S"))
        scoring_log.addHandler(sh)

    if stream_file:
        stream_logger = logging.getLogger("balatro_stream")
        stream_logger.setLevel(logging.INFO)
        stream_logger.propagate = False
        stream_fmt = logging.Formatter("%(message)s")
        sfh = logging.FileHandler(stream_file, mode="a", encoding="utf-8")
        sfh.setFormatter(stream_fmt)
        stream_logger.addHandler(sfh)
        # Also print to console for OBS window capture
        sc = logging.StreamHandler()
        sc.setFormatter(stream_fmt)
        stream_logger.addHandler(sc)


def wait_for_server(client: BalatroClient, timeout: float = 30.0) -> None:
    """Block until the balatrobot API responds."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            health = read_health(client.call("health"))
            client.responses_settled = health.settled
            client.simulator = health.simulator
            log.info("Connected to balatrobot API")
            return
        except Exception:
            time.sleep(0.5)
    raise TimeoutError(f"balatrobot API not reachable after {timeout}s")


# ---------------------------------------------------------------------------
# Main game loop
# ---------------------------------------------------------------------------

def run_bot(
    client: BalatroClient,
    engine: RuleEngine,
    *,
    start_game: bool = False,
    deck: str = "RED",
    stake: str = "WHITE",
    seed: str | None = None,
    poll_interval: float = 0.2,
    stream_delay: float = 0.0,
    dashboard_batch_id: int | None = None,
    replays: ReplayWriter | None = None,
    report: Reporter | None = None,
) -> bool:
    """Main bot loop. Returns True if the game was won.

    With *replays*, a simulator game started here is saved as a replay, and the
    dashboard then gets the game's summary without the detail the replay can rebuild.
    With *report*, the game's start, moves and end go to the instance report.
    """

    if start_game:
        try:
            client.call("menu")
        except APIError:
            pass
        start_params: dict[str, object] = {"deck": deck, "stake": stake}
        if seed is not None:
            start_params["seed"] = seed
        for _attempt in range(3):
            try:
                state = client.call("start", start_params)
                break
            except (APIError, httpx.HTTPError) as e:
                reason = e.message if isinstance(e, APIError) else f"{type(e).__name__}: {e}"
                log.warning("start() failed (attempt %d): %s — retrying", _attempt + 1, reason)
                time.sleep(1)
                try:
                    client.call("menu")
                except (APIError, httpx.HTTPError):
                    pass
                time.sleep(1)
        else:
            raise RuntimeError("Failed to start game after 3 attempts")
        log.info("Started new game: deck=%s stake=%s seed=%s", deck, stake, state.get("seed"))
        if replays is not None and client.simulator:
            client.recording = [["start", {**start_params, "seed": state.get("seed")}]]
        _stream_log.info("New game — Seed: %s | Deck: %s", state.get("seed"), deck)
    else:
        state = client.call("gamestate")
        log.info("Joined existing game: state=%s", state.get("state"))

    explorer().reseed(state.get("seed"))
    if report is not None:
        report.game_started(explorer().rate)
    seed_draws(state.get("seed"))
    recorder = GameRecorder()
    dispatcher = Dispatcher(client, engine, poll_interval=poll_interval, stream_delay=stream_delay)

    while state.get("state") != "GAME_OVER":
        game_state = state.get("state", "")
        recorder.observe(state)
        if game_state in PASSING_STATES:
            state = dispatcher.wait(state)
            continue

        # The live game's action response may have stale money, joker counters or debuff flags.
        if game_state == "SELECTING_HAND" and not getattr(client, "responses_settled", False):
            state = dispatcher.poll(state)

        state["_live_stats"] = recorder.live_stats()
        action = engine.decide(state)
        if action is None:
            log.debug("No rule matched for state=%s, polling...", game_state)
            state = dispatcher.wait(state)
            continue

        method, params = action.to_rpc()
        state = dispatcher.prepare(method, params, state)
        recorder.sending(game_state, method, params, action, engine.last_decision, state)
        if report is not None:
            report.action(recorder.actions_taken, game_state, method, params)
        sent = dispatcher.send(game_state, method, params, state)
        state = sent.state
        if sent.outcome == "acted":
            recorder.acted(state, sent.recording_at)
        elif sent.outcome == "refused":
            recorder.refused(sent.error)
        else:
            recorder.timed_out()

    recorder.finish(state)
    if report is not None:
        report.game_finished(recorder.won, state.get("seed", ""), state.get("ante_num") or 0)
    # Taken after the game-over summary is logged, for wins and losses alike.
    log_text = None
    if _win_handler:
        if recorder.won:
            _win_handler.flush_win(state.get("seed", "?"))
        log_text = _win_handler.get_log_text()
        _win_handler.reset()

    replayable = client.recording is not None
    if replayable:
        replays.write(replay_record(
            actions=client.recording, explored=recorder.explored_actions,
            seed=state.get("seed", ""), deck=deck, stake=stake, simulator=client.simulator,
            won=recorder.won, final_state=state, batch=dashboard_batch_id,
            explore_rate=explorer().rate,
        ))
        client.recording = None

    if dashboard_batch_id:
        from balatro_bot import dashboard_client
        dashboard_client.post_game(dashboard_batch_id, recorder.payload(
            state, replayable=replayable, port=client.port, deck=deck, stake=stake, log_text=log_text,
        ))

    return recorder.won
