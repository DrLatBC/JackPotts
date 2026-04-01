#!/usr/bin/env python3
"""Batch stats aggregator for Balatro bot runs.

Usage:
    python stats.py                    # latest batch across all instances
    python stats.py 018                # specific batch number
    python stats.py 018 019 020        # compare multiple batches
    python stats.py --dir bot_log      # custom log directory
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .finder import find_instance_dirs, find_latest_batch
from .parser import parse_game_log
from .scoring import parse_scoring_log
from .merge import merge_stats
from .report import generate_markdown, _pct
from .replay import find_winning_games, parse_win_game, generate_win_replay_md


def main():
    parser = argparse.ArgumentParser(description="Balatro bot batch stats")
    parser.add_argument("batches", nargs="*", help="Batch numbers (e.g. 018 019). Default: latest")
    parser.add_argument("--dir", default="bot_log", help="Log directory")
    args = parser.parse_args()

    log_dir = Path(args.dir)
    if not log_dir.exists():
        print(f"Log directory not found: {log_dir}")
        sys.exit(1)

    instances = find_instance_dirs(log_dir)
    if not instances:
        print(f"No instance directories found in {log_dir}")
        sys.exit(1)

    # Determine which batches to analyze
    if args.batches:
        batch_nums = [b.zfill(3) for b in args.batches]
    else:
        latest = find_latest_batch(log_dir)
        if not latest:
            print("No game logs found")
            sys.exit(1)
        batch_nums = [latest]

    # Output directory
    out_dir = log_dir / "stats_output"
    out_dir.mkdir(exist_ok=True)

    for batch_num in batch_nums:
        all_stats = []
        for inst in instances:
            log_file = inst / f"game_{batch_num}.log"
            if log_file.exists():
                game_stats = parse_game_log(log_file)

                # Also parse scoring log if it exists
                scoring_file = inst / f"scoring_{batch_num}.log"
                scoring_stats = parse_scoring_log(scoring_file)
                game_stats.update(scoring_stats)

                all_stats.append(game_stats)

        if not all_stats:
            print(f"No logs found for batch {batch_num}")
            continue

        merged = merge_stats(all_stats)
        md = generate_markdown(merged, f"Batch {batch_num}")

        # Write markdown file keyed by batches being compared
        out_file = out_dir / f"batch_{batch_num}.md"
        out_file.write_text(md, encoding="utf-8")

        # Print summary to terminal
        games = merged["games"]
        total = len(games)
        wins = merged["wins"]
        antes = [g["ante"] for g in games]
        avg_ante = sum(antes) / len(antes) if antes else 0
        print(f"Batch {batch_num}: {total} games, {wins} wins ({_pct(wins, total)}), avg ante {avg_ante:.1f}")
        print(f"  -> {out_file}")

        # Win replay narrative
        if wins > 0:
            log_files_for_replay = [
                inst / f"game_{batch_num}.log"
                for inst in instances
                if (inst / f"game_{batch_num}.log").exists()
            ]
            win_game_chunks: list[list[str]] = []
            for lf in log_files_for_replay:
                win_game_chunks.extend(find_winning_games(lf))
            if win_game_chunks:
                replays = [parse_win_game(chunk) for chunk in win_game_chunks]
                replay_md = generate_win_replay_md(f"Batch {batch_num}", replays)
                replay_path = out_dir / f"batch_{batch_num}_wins.md"
                replay_path.write_text(replay_md, encoding="utf-8")
                print(f"  -> {replay_path} ({len(replays)} win{'s' if len(replays) != 1 else ''})")


if __name__ == "__main__":
    main()
