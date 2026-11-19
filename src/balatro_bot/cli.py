"""CLI entry point for the Balatro bot."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from balatro_bot.infrastructure.client import BalatroClient

from balatro_bot import instance_report
from balatro_bot.bot import run_bot, setup_logging, wait_for_server
from balatro_bot.domain.policy import exploration
from balatro_bot.engine import RuleEngine
from balatro_bot.replays import ReplayWriter

import logging
log = logging.getLogger("balatro_bot")


def apply_explore_file(path: Path | None) -> None:
    """Switch to the rate in *path* before a game."""
    if path is None:
        return
    try:
        rate = round(float(path.read_text().strip()), 2)
    except (OSError, ValueError):
        return
    if not 0 <= rate <= 1 or rate == exploration.explorer().rate:
        return
    exploration.configure(rate)
    log.info("Explore rate: %.2f", rate)


def main() -> None:
    parser = argparse.ArgumentParser(description="Balatro decision engine bot")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=12346)
    parser.add_argument("--start", action="store_true", help="Start a new game")
    parser.add_argument("--deck", default="RED", help="Deck to use (default: RED)")
    parser.add_argument("--stake", default="WHITE", help="Stake level (default: WHITE)")
    parser.add_argument("--seed", default=None, help="Force a specific seed")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--games", type=int, default=1, help="Number of games to play")
    parser.add_argument("--games-offset", type=int, default=0, help="Games already completed (numbers the game headers on restart)")
    parser.add_argument("--batch-number", type=int, default=None,
                        help="Batch number naming the log files (default: one past the highest local one)")
    parser.add_argument("--log", default=None, help="Log file (default: growing_{port}.txt)")
    parser.add_argument("--stream-delay", type=float, default=0.0,
                        help="Seconds to pause after each bot action (used by supervisor --stream)")
    parser.add_argument("--stream-log", action="store_true",
                        help="Route logs to bot_log/stream/ instead of bot_log/{port}/")
    parser.add_argument("--dashboard-batch-id", type=int, default=None,
                        help="Dashboard batch ID for reporting game results")
    parser.add_argument("--explore", type=float, default=0.0,
                        help="Fraction of shop and pack decisions that take a top-5 runner-up (default: 0)")
    parser.add_argument("--explore-file", type=Path, default=None,
                        help="File holding an explore rate, re-read before each game (written by the supervisor)")
    args = parser.parse_args()
    exploration.configure(args.explore)

    number = args.batch_number or instance_report.next_local_number()
    files = instance_report.batch_files(args.port, number, stream=args.stream_log)
    log_dir = files.game_log.parent
    log_dir.mkdir(parents=True, exist_ok=True)
    files.wins_log.parent.mkdir(parents=True, exist_ok=True)

    log_file = args.log or str(files.game_log)
    stream_file = str(log_dir / f"stream_{number:03d}.log") if args.stream_log else None
    setup_logging(args.verbose, log_file=log_file, wins_file=str(files.wins_log),
                  scoring_file=str(files.scoring_log), stream_file=stream_file)

    replays: ReplayWriter | None = None
    report = instance_report.Reporter(files.report)
    try:
        client = BalatroClient(host=args.host, port=args.port)
        engine = RuleEngine()

        wait_for_server(client)
        # Simulator games are kept as replays beside the instance's logs.
        if client.simulator:
            replays = ReplayWriter(files.replay)

        wins = 0
        offset = args.games_offset
        total_games = offset + args.games
        if args.stream_log:
            (log_dir / "wins.txt").write_text(f"W: 0  L: 0")
        i = 0
        while i < args.games:
            apply_explore_file(args.explore_file)
            if args.games > 1:
                log.info("=== Game %d/%d ===", offset + i + 1, total_games)
            won = run_bot(
                client, engine,
                start_game=args.start,
                deck=args.deck,
                stake=args.stake,
                seed=args.seed,
                stream_delay=args.stream_delay,
                dashboard_batch_id=args.dashboard_batch_id,
                replays=replays,
                report=report,
            )
            if won:
                wins += 1
            if args.stream_log:
                games_played = offset + i + 1
                (log_dir / "wins.txt").write_text(f"W: {wins}  L: {games_played - wins}")
            i += 1

        if args.games > 1:
            log.info("Results: %d/%d wins (%.0f%%)", wins, args.games, 100 * wins / args.games)

    except Exception:
        log.exception("FATAL: bot crashed")
        sys.exit(1)
    finally:
        report.close()
        if replays is not None:
            replays.close()


if __name__ == "__main__":
    main()
