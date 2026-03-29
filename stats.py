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
import os
import re
import sys
from collections import Counter
from pathlib import Path


def find_instance_dirs(log_dir: Path) -> list[Path]:
    """Find all instance directories (port numbers)."""
    return sorted(
        p for p in log_dir.iterdir()
        if p.is_dir() and p.name.isdigit()
    )


def find_latest_batch(log_dir: Path) -> str | None:
    """Find the highest batch number across all instances."""
    highest = 0
    for inst in find_instance_dirs(log_dir):
        for f in inst.glob("game_*.log"):
            num = int(f.stem.split("_")[1])
            highest = max(highest, num)
    return f"{highest:03d}" if highest > 0 else None


def parse_game_log(path: Path) -> dict:
    """Parse a single game log file and extract stats."""
    text = path.read_text(encoding="utf-8", errors="replace")

    games = []
    ante_deaths = Counter()
    boss_kills = Counter()
    joker_buys = Counter()
    utility_buys = Counter()
    xmult_buys = Counter()
    synergy_buys = 0
    milk_actions = Counter()
    luchador_sells = 0
    invisible_sells = 0
    diet_cola_sells = 0
    wins = 0

    UTILITY_JOKERS = {
        "Juggler", "Drunkard", "Four Fingers", "Smeared Joker", "Shortcut",
        "Splash", "Chicot", "Mr. Bones", "Perkeo", "Cartomancer",
        "Hallucination", "Space Joker", "8 Ball", "Oops! All 6s",
        "Pareidolia", "Hack", "Seltzer", "Invisible Joker", "Luchador",
        "Diet Cola", "Turtle Bean", "Burnt Joker", "Merry Andy",
        "Four Fingers", "Shortcut",
    }
    XMULT_JOKERS = {
        "Cavendish", "Joker Stencil", "The Duo", "The Trio", "The Family",
        "The Order", "The Tribe", "Acrobat", "Blackboard", "Flower Pot",
        "Madness", "Constellation", "Campfire", "Vampire", "Hologram",
    }

    for line in text.splitlines():
        # Game over
        m = re.search(r"Game over: (\w+).*ante=(\d+).*round=(\d+).*?(\d+) actions", line)
        if m:
            result, ante, rnd, actions = m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4))
            is_win = "VICTORY" in line
            if is_win:
                wins += 1
            games.append({"result": result, "ante": ante, "round": rnd, "actions": actions, "win": is_win})
            if not is_win:
                ante_deaths[ante] += 1

        # Boss blind that preceded a death — look for Blind: lines
        if "Blind:" in line and "Small Blind" not in line and "Big Blind" not in line:
            bm = re.search(r"Blind: (.+?) \(need", line)
            if bm:
                last_boss = bm.group(1)

        # Joker purchases
        jm = re.search(r"buy joker: (.+?) for \$", line)
        if jm:
            name = jm.group(1)
            joker_buys[name] += 1
            if name in UTILITY_JOKERS:
                utility_buys[name] += 1
            if name in XMULT_JOKERS:
                xmult_buys[name] += 1

        # Synergy buys
        if "synergy=" in line and "BUYING" in line:
            synergy_buys += 1

        # Milk actions
        if "milk:" in line:
            mm = re.search(r"milk: (.+?) \(", line)
            if mm:
                milk_actions[mm.group(1)] += 1

        # Triggered utility
        if "Luchador: sell" in line:
            luchador_sells += 1
        if "Invisible:" in line and "sell" in line:
            invisible_sells += 1
        if "Diet Cola: sell" in line:
            diet_cola_sells += 1

    return {
        "games": games,
        "wins": wins,
        "ante_deaths": ante_deaths,
        "joker_buys": joker_buys,
        "utility_buys": utility_buys,
        "xmult_buys": xmult_buys,
        "synergy_buys": synergy_buys,
        "milk_actions": milk_actions,
        "luchador_sells": luchador_sells,
        "invisible_sells": invisible_sells,
        "diet_cola_sells": diet_cola_sells,
    }


def merge_stats(all_stats: list[dict]) -> dict:
    """Merge stats from multiple log files."""
    merged = {
        "games": [],
        "wins": 0,
        "ante_deaths": Counter(),
        "joker_buys": Counter(),
        "utility_buys": Counter(),
        "xmult_buys": Counter(),
        "synergy_buys": 0,
        "milk_actions": Counter(),
        "luchador_sells": 0,
        "invisible_sells": 0,
        "diet_cola_sells": 0,
    }
    for s in all_stats:
        merged["games"].extend(s["games"])
        merged["wins"] += s["wins"]
        merged["ante_deaths"] += s["ante_deaths"]
        merged["joker_buys"] += s["joker_buys"]
        merged["utility_buys"] += s["utility_buys"]
        merged["xmult_buys"] += s["xmult_buys"]
        merged["synergy_buys"] += s["synergy_buys"]
        merged["milk_actions"] += s["milk_actions"]
        merged["luchador_sells"] += s["luchador_sells"]
        merged["invisible_sells"] += s["invisible_sells"]
        merged["diet_cola_sells"] += s["diet_cola_sells"]
    return merged


def print_stats(stats: dict, batch_label: str) -> None:
    """Print formatted stats summary."""
    games = stats["games"]
    total = len(games)
    wins = stats["wins"]

    if total == 0:
        print(f"=== {batch_label}: No games found ===")
        return

    antes = [g["ante"] for g in games]
    avg_ante = sum(antes) / len(antes)
    sorted_antes = sorted(antes)
    median_ante = sorted_antes[len(sorted_antes) // 2]

    print(f"\n{'=' * 60}")
    print(f"  {batch_label} — {total} games, {wins} wins ({100*wins/total:.1f}%)")
    print(f"  Avg ante: {avg_ante:.1f} | Median: {median_ante}")
    print(f"{'=' * 60}")

    # Deaths by ante
    print("\n  Deaths by ante:")
    max_deaths = max(stats["ante_deaths"].values()) if stats["ante_deaths"] else 1
    for ante in sorted(stats["ante_deaths"]):
        count = stats["ante_deaths"][ante]
        pct = 100 * count / total
        bar = "#" * int(20 * count / max_deaths)
        print(f"    {ante}: {count:>3} ({pct:4.1f}%) {bar}")

    # Top joker purchases
    print(f"\n  Top joker purchases:")
    for name, count in stats["joker_buys"].most_common(10):
        print(f"    {name}: {count}")

    # xMult purchases
    xmult_total = sum(stats["xmult_buys"].values())
    if xmult_total:
        print(f"\n  xMult jokers bought: {xmult_total}")
        for name, count in stats["xmult_buys"].most_common(5):
            print(f"    {name}: {count}")

    # Utility purchases
    util_total = sum(stats["utility_buys"].values())
    if util_total:
        print(f"\n  Utility jokers bought: {util_total}")
        for name, count in stats["utility_buys"].most_common(5):
            print(f"    {name}: {count}")

    # Synergy buys
    if stats["synergy_buys"]:
        print(f"\n  Cross-synergy boosted purchases: {stats['synergy_buys']}")

    # Triggered utility
    triggered = stats["luchador_sells"] + stats["invisible_sells"] + stats["diet_cola_sells"]
    if triggered:
        print(f"\n  Triggered utility actions: {triggered}")
        if stats["luchador_sells"]:
            print(f"    Luchador sells: {stats['luchador_sells']}")
        if stats["invisible_sells"]:
            print(f"    Invisible sells: {stats['invisible_sells']}")
        if stats["diet_cola_sells"]:
            print(f"    Diet Cola sells: {stats['diet_cola_sells']}")

    # Milk actions
    milk_total = sum(stats["milk_actions"].values())
    if milk_total:
        print(f"\n  Milk actions: {milk_total}")
        for action, count in stats["milk_actions"].most_common(5):
            print(f"    {action}: {count}")

    print()


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

    for batch_num in batch_nums:
        all_stats = []
        for inst in instances:
            log_file = inst / f"game_{batch_num}.log"
            if log_file.exists():
                all_stats.append(parse_game_log(log_file))

        if not all_stats:
            print(f"No logs found for batch {batch_num}")
            continue

        merged = merge_stats(all_stats)
        print_stats(merged, f"Batch {batch_num}")


if __name__ == "__main__":
    main()
