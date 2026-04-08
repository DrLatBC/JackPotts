"""CLI entry point for the Balatro bot."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from balatrobot import BalatroClient

from balatro_bot.bot import run_bot, setup_logging, wait_for_server
from balatro_bot.engine import RuleEngine

import logging
log = logging.getLogger("balatro_bot")


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
    parser.add_argument("--games-offset", type=int, default=0, help="Games already completed (for progress tracking on restart)")
    parser.add_argument("--log", default=None, help="Log file (default: growing_{port}.txt)")
    args = parser.parse_args()

    log_dir = Path("bot_log") / str(args.port)
    wins_dir = Path("bot_log") / "wins"
    log_dir.mkdir(parents=True, exist_ok=True)
    wins_dir.mkdir(exist_ok=True)

    num_file = Path("bot_log") / "next_num.txt"
    if num_file.exists():
        session_num = int(num_file.read_text().strip())
    else:
        all_nums = [
            int(m.group(1))
            for p in Path("bot_log").glob("*/game_*.log")
            if (m := re.match(r"game_(\d+)\.log", p.name))
        ]
        session_num = (max(all_nums) + 1) if all_nums else 1
    port_nums = [
        int(m.group(1))
        for p in log_dir.glob("game_*.log")
        if (m := re.match(r"game_(\d+)\.log", p.name))
    ]
    next_num = max(session_num, max(port_nums)) if port_nums else session_num

    if args.log:
        log_file = args.log
    else:
        log_file = str(log_dir / f"game_{next_num:03d}.log")

    wins_file = str(wins_dir / f"wins_{args.port}_{next_num:03d}.log")
    scoring_file = str(log_dir / f"scoring_{next_num:03d}.log")
    progress_file = log_dir / "progress.txt"
    setup_logging(args.verbose, log_file=log_file, wins_file=wins_file, scoring_file=scoring_file)

    try:
        client = BalatroClient(host=args.host, port=args.port)
        engine = RuleEngine()

        wait_for_server(client)

        wins = 0
        offset = args.games_offset
        total_games = offset + args.games
        progress_file.write_text(f"{offset}/{total_games}")
        i = 0
        while i < args.games:
            if args.games > 1:
                log.info("=== Game %d/%d ===", offset + i + 1, total_games)
            won = run_bot(
                client, engine,
                start_game=args.start,
                deck=args.deck,
                stake=args.stake,
                seed=args.seed,
            )
            if won:
                wins += 1
            progress_file.write_text(f"{offset + i + 1}/{total_games}")
            i += 1

        if args.games > 1:
            log.info("Results: %d/%d wins (%.0f%%)", wins, args.games, 100 * wins / args.games)

    except Exception:
        log.exception("FATAL: bot crashed")
        sys.exit(1)


if __name__ == "__main__":
    main()
