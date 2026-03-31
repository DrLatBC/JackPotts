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
import re
import sys
from collections import Counter, defaultdict
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


UTILITY_JOKERS = {
    "Juggler", "Drunkard", "Four Fingers", "Smeared Joker", "Shortcut",
    "Splash", "Chicot", "Mr. Bones", "Perkeo", "Cartomancer",
    "Hallucination", "Space Joker", "8 Ball", "Oops! All 6s",
    "Pareidolia", "Hack", "Seltzer", "Invisible Joker", "Luchador",
    "Diet Cola", "Turtle Bean", "Burnt Joker", "Merry Andy",
}
XMULT_JOKERS = {
    "Cavendish", "Joker Stencil", "The Duo", "The Trio", "The Family",
    "The Order", "The Tribe", "Acrobat", "Blackboard", "Flower Pot",
    "Madness", "Constellation", "Campfire", "Vampire", "Hologram",
}

# Regex patterns compiled once
RE_GAME_OVER = re.compile(r"Game over: (\w+).*ante=(\d+).*round=(\d+).*?(\d+) actions")
RE_SUMMARY = re.compile(r"Summary: \$(\d+) \| jokers: \[([^\]]*)\] \| hands=(\d+) discards=(\d+)")
RE_ROUND = re.compile(
    r"\[ROUND\] (.+?): scored ([\d,]+) / needed ([\d,]+)"
    r" .+ (WON|LOST) \| (\d+) hands?, (\d+) discards?"
)
RE_PLAY = re.compile(r"SELECTING_HAND -> play\(.+?\| (.+?) for (\d+)")
RE_BEST_AVAILABLE = re.compile(r"best available: (.+?) for (\d+)")
RE_MILK_PLAY = re.compile(r"milk: (.+?) for (\d+)")
RE_MOUTH_LOCKED = re.compile(r"playing (.+?) for (\d+)")

KNOWN_HAND_TYPES = {
    "High Card", "Pair", "Two Pair", "Three of a Kind", "Straight",
    "Flush", "Full House", "Four of a Kind", "Straight Flush",
    "Five of a Kind", "Flush House", "Flush Five",
}
RE_JOKER_BUY = re.compile(r"buy joker: (.+?) for \$")
RE_VOUCHER_BUY = re.compile(r"buy voucher: (.+?) for \$")
RE_BLIND = re.compile(r"Blind: (.+?) \(need")
RE_MILK_ACTION = re.compile(r"milk: (.+?) \(")
RE_REROLL = re.compile(r"SHOP -> reroll\(\)")


def _parse_comma_int(s: str) -> int:
    return int(s.replace(",", ""))


def parse_game_log(path: Path) -> dict:
    """Parse a single game log file and extract stats."""
    text = path.read_text(encoding="utf-8", errors="replace")

    games = []
    ante_deaths = Counter()
    joker_buys = Counter()
    utility_buys = Counter()
    xmult_buys = Counter()
    synergy_buys = 0
    milk_actions = Counter()
    luchador_sells = 0
    invisible_sells = 0
    diet_cola_sells = 0
    wins = 0

    # New metrics
    round_results = []          # list of dicts per round
    hand_types_played = Counter()
    killing_blinds = Counter()  # blind name that ended each lost game
    voucher_buys = Counter()
    final_money = []            # money at game end
    final_jokers_wins = Counter()   # jokers present in winning games
    final_jokers_losses = Counter() # jokers present in losing games
    rerolls = 0

    # Tracking state across lines
    last_round_result = None    # last [ROUND]...LOST line's blind name
    last_hand_type = None       # track last played hand type for round-ending plays
    cur_ante = 0                # current ante number for round tracking

    for line in text.splitlines():
        # Ante tracking
        if "[ANTE " in line and "Roster" in line:
            am = re.search(r"\[ANTE (\d+)\]", line)
            if am:
                cur_ante = int(am.group(1))

        # Blind announcement (track ante for rounds that happen before first ANTE line)
        bm_line = RE_BLIND.search(line)
        if bm_line and "Blind:" in line:
            blind_name = bm_line.group(1)

        # Round results
        rm = RE_ROUND.search(line)
        if rm:
            blind_name = rm.group(1)
            scored = _parse_comma_int(rm.group(2))
            needed = _parse_comma_int(rm.group(3))
            won = rm.group(4) == "WON"
            hands_used = int(rm.group(5))
            discards_used = int(rm.group(6))
            is_boss = blind_name not in ("Small Blind", "Big Blind")

            round_results.append({
                "blind": blind_name,
                "scored": scored,
                "needed": needed,
                "won": won,
                "hands": hands_used,
                "discards": discards_used,
                "ante": cur_ante,
                "is_boss": is_boss,
            })

            if not won:
                last_round_result = blind_name

            # Track last hand type for this round
            if won and last_hand_type:
                pass  # already tracked in hand_types_played

        # Hand type played — extract from play() actions
        pm = RE_PLAY.search(line)
        if pm:
            hand_type = pm.group(1)
            # Handle "best available: Type for N" format
            if hand_type.startswith("best available"):
                bam = RE_BEST_AVAILABLE.search(line)
                if bam:
                    hand_type = bam.group(1)
            # Handle "milk: Type for joker" format
            elif hand_type.startswith("milk"):
                mlm = RE_MILK_PLAY.search(line)
                if mlm:
                    hand_type = mlm.group(1)
            # Handle "mouth locked (X) but can't form it: playing Type for N"
            elif "mouth locked" in hand_type:
                mlk = RE_MOUTH_LOCKED.search(line)
                if mlk:
                    hand_type = mlk.group(1)
            hand_type = hand_type.strip()
            if hand_type in KNOWN_HAND_TYPES:
                hand_types_played[hand_type] += 1
                last_hand_type = hand_type

        # Game over
        m = RE_GAME_OVER.search(line)
        if m:
            result = m.group(1)
            ante = int(m.group(2))
            rnd = int(m.group(3))
            actions = int(m.group(4))
            is_win = "VICTORY" in line
            if is_win:
                wins += 1
            games.append({
                "result": result, "ante": ante, "round": rnd,
                "actions": actions, "win": is_win,
            })
            if not is_win:
                ante_deaths[ante] += 1
                if last_round_result:
                    killing_blinds[last_round_result] += 1
            last_round_result = None
            last_hand_type = None
            cur_ante = 0

        # Summary line (follows Game over)
        sm = RE_SUMMARY.search(line)
        if sm:
            money = int(sm.group(1))
            jokers_str = sm.group(2).strip()
            final_money.append(money)

            if jokers_str:
                joker_list = [j.strip() for j in jokers_str.split(",")]
                # Clean joker names — strip scaling suffixes like "(+52mult)"
                joker_names = []
                for j in joker_list:
                    clean = re.sub(r"\(.*?\)", "", j).strip()
                    if clean:
                        joker_names.append(clean)

                # Determine if this was a win or loss (check last game appended)
                if games and games[-1]["win"]:
                    for j in joker_names:
                        final_jokers_wins[j] += 1
                else:
                    for j in joker_names:
                        final_jokers_losses[j] += 1

        # Joker purchases
        jm = RE_JOKER_BUY.search(line)
        if jm:
            name = jm.group(1)
            joker_buys[name] += 1
            if name in UTILITY_JOKERS:
                utility_buys[name] += 1
            if name in XMULT_JOKERS:
                xmult_buys[name] += 1

        # Voucher purchases
        vm = RE_VOUCHER_BUY.search(line)
        if vm:
            voucher_buys[vm.group(1)] += 1

        # Synergy buys
        if "synergy=" in line and "BUYING" in line:
            synergy_buys += 1

        # Milk actions
        if "milk:" in line:
            mm = RE_MILK_ACTION.search(line)
            if mm:
                milk_actions[mm.group(1)] += 1

        # Rerolls
        if RE_REROLL.search(line):
            rerolls += 1

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
        "round_results": round_results,
        "hand_types_played": hand_types_played,
        "killing_blinds": killing_blinds,
        "voucher_buys": voucher_buys,
        "final_money": final_money,
        "final_jokers_wins": final_jokers_wins,
        "final_jokers_losses": final_jokers_losses,
        "rerolls": rerolls,
    }


def parse_scoring_log(path: Path) -> dict:
    """Parse a scoring log for estimate vs actual accuracy."""
    if not path.exists():
        return {"total_scores": 0, "mismatches": 0, "mismatch_diffs": []}

    text = path.read_text(encoding="utf-8", errors="replace")
    total = 0
    mismatches = 0
    diffs = []

    for line in text.splitlines():
        if "est=" in line and "actual=" in line:
            total += 1
            m = re.search(r"MISMATCH\(diff=(-?\d+)\)", line)
            if m:
                mismatches += 1
                diffs.append(int(m.group(1)))

    return {"total_scores": total, "mismatches": mismatches, "mismatch_diffs": diffs}


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
        "round_results": [],
        "hand_types_played": Counter(),
        "killing_blinds": Counter(),
        "voucher_buys": Counter(),
        "final_money": [],
        "final_jokers_wins": Counter(),
        "final_jokers_losses": Counter(),
        "rerolls": 0,
        "total_scores": 0,
        "mismatches": 0,
        "mismatch_diffs": [],
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
        merged["round_results"].extend(s.get("round_results", []))
        merged["hand_types_played"] += s.get("hand_types_played", Counter())
        merged["killing_blinds"] += s.get("killing_blinds", Counter())
        merged["voucher_buys"] += s.get("voucher_buys", Counter())
        merged["final_money"].extend(s.get("final_money", []))
        merged["final_jokers_wins"] += s.get("final_jokers_wins", Counter())
        merged["final_jokers_losses"] += s.get("final_jokers_losses", Counter())
        merged["rerolls"] += s.get("rerolls", 0)
        merged["total_scores"] += s.get("total_scores", 0)
        merged["mismatches"] += s.get("mismatches", 0)
        merged["mismatch_diffs"].extend(s.get("mismatch_diffs", []))
    return merged


def _bar(n: int, total: int, width: int = 20) -> str:
    """Unicode bar chart segment."""
    if total == 0:
        return ""
    filled = max(1, round(width * n / total))
    return "\u2588" * filled + "\u2591" * (width - filled)


def _pct(n: int, total: int) -> str:
    if total == 0:
        return "0.0%"
    return f"{100 * n / total:.1f}%"


def generate_markdown(stats: dict, batch_label: str) -> str:
    """Generate a markdown report from parsed stats."""
    lines: list[str] = []
    w = lines.append  # shorthand

    games = stats["games"]
    total = len(games)
    wins = stats["wins"]
    losses = total - wins
    rounds = stats["round_results"]

    if total == 0:
        return f"# {batch_label}\n\nNo games found.\n"

    antes = [g["ante"] for g in games]
    avg_ante = sum(antes) / len(antes)
    sorted_antes = sorted(antes)
    median_ante = sorted_antes[len(sorted_antes) // 2]
    max_ante = max(antes)

    # ═══════════════════════════════════════════════════════════════
    # Header
    # ═══════════════════════════════════════════════════════════════
    w(f"# {batch_label}")
    w("")
    w(f"| Stat | Value |")
    w(f"|------|-------|")
    w(f"| Games | {total} |")
    w(f"| Wins | {wins} ({_pct(wins, total)}) |")
    w(f"| Avg Ante | {avg_ante:.1f} |")
    w(f"| Median Ante | {median_ante} |")
    w(f"| Max Ante | {max_ante} |")
    if stats["final_money"]:
        avg_money = sum(stats["final_money"]) / len(stats["final_money"])
        med_money = sorted(stats["final_money"])[len(stats["final_money"]) // 2]
        w(f"| Avg Final $ | ${avg_money:.0f} |")
        w(f"| Median Final $ | ${med_money} |")
    w(f"| Total Rounds | {len(rounds)} |")
    w(f"| Rerolls | {stats['rerolls']} |")

    # ═══════════════════════════════════════════════════════════════
    # Deaths by Ante
    # ═══════════════════════════════════════════════════════════════
    if stats["ante_deaths"]:
        w("")
        w("## Deaths by Ante")
        w("")
        max_deaths = max(stats["ante_deaths"].values())
        w("| Ante | Deaths | % | |")
        w("|------|--------|---|---|")
        for ante in sorted(stats["ante_deaths"]):
            count = stats["ante_deaths"][ante]
            bar = _bar(count, max_deaths)
            w(f"| {ante} | {count} | {_pct(count, total)} | `{bar}` |")

    # ═══════════════════════════════════════════════════════════════
    # Killing Blind
    # ═══════════════════════════════════════════════════════════════
    if stats["killing_blinds"]:
        w("")
        w("## Killing Blind")
        w("")
        w("What specific blind ended each lost run.")
        w("")
        w("| Blind | Deaths | % of Losses |")
        w("|-------|--------|-------------|")
        for name, count in stats["killing_blinds"].most_common(15):
            w(f"| {name} | {count} | {_pct(count, losses)} |")

    # ═══════════════════════════════════════════════════════════════
    # Boss Blind Performance
    # ═══════════════════════════════════════════════════════════════
    boss_rounds = [r for r in rounds if r["is_boss"]]
    if boss_rounds:
        w("")
        w("## Boss Blind Performance")
        w("")
        boss_stats: dict[str, dict] = {}
        for r in boss_rounds:
            name = r["blind"]
            if name not in boss_stats:
                boss_stats[name] = {"wins": 0, "losses": 0}
            if r["won"]:
                boss_stats[name]["wins"] += 1
            else:
                boss_stats[name]["losses"] += 1

        sorted_bosses = sorted(
            boss_stats.items(),
            key=lambda x: x[1]["wins"] + x[1]["losses"],
            reverse=True,
        )
        w("| Boss | W | L | Total | Win% |")
        w("|------|---|---|-------|------|")
        for name, bs in sorted_bosses:
            t = bs["wins"] + bs["losses"]
            wr = 100 * bs["wins"] / t if t > 0 else 0
            flag = " :warning:" if wr < 60 else ""
            w(f"| {name} | {bs['wins']} | {bs['losses']} | {t} | {wr:.0f}%{flag} |")

    # ═══════════════════════════════════════════════════════════════
    # Round Efficiency
    # ═══════════════════════════════════════════════════════════════
    won_rounds = [r for r in rounds if r["won"]]
    if won_rounds:
        w("")
        w("## Round Efficiency")
        w("")

        one_shots = sum(1 for r in won_rounds if r["hands"] == 1)
        avg_hands = sum(r["hands"] for r in won_rounds) / len(won_rounds)
        close = [r for r in won_rounds if r["needed"] > 0 and r["scored"] / r["needed"] < 1.15]

        w(f"| Metric | Value |")
        w(f"|--------|-------|")
        w(f"| One-shot rate | {one_shots}/{len(won_rounds)} ({_pct(one_shots, len(won_rounds))}) |")
        w(f"| Avg hands/round | {avg_hands:.1f} |")
        w(f"| Close calls (<15% margin) | {len(close)} ({_pct(len(close), len(won_rounds))}) |")

        # Overkill by ante
        ante_overkill: dict[int, list] = defaultdict(list)
        for r in won_rounds:
            if r["needed"] > 0 and r["ante"] > 0:
                ante_overkill[r["ante"]].append(r["scored"] / r["needed"])
        if ante_overkill:
            w("")
            w("### Scoring Headroom by Ante")
            w("")
            w("Average (scored / needed) for won rounds.")
            w("")
            w("| Ante | Overkill | Rounds |")
            w("|------|----------|--------|")
            for ante in sorted(ante_overkill):
                vals = ante_overkill[ante]
                avg_ok = sum(vals) / len(vals)
                w(f"| {ante} | {avg_ok:.1f}x | {len(vals)} |")

    # ═══════════════════════════════════════════════════════════════
    # Hand Types Played
    # ═══════════════════════════════════════════════════════════════
    if stats["hand_types_played"]:
        w("")
        w("## Hand Types Played")
        w("")
        total_plays = sum(stats["hand_types_played"].values())
        w("| Hand Type | Count | % |")
        w("|-----------|-------|---|")
        for ht, count in stats["hand_types_played"].most_common():
            w(f"| {ht} | {count} | {_pct(count, total_plays)} |")

    # ═══════════════════════════════════════════════════════════════
    # Joker Purchases
    # ═══════════════════════════════════════════════════════════════
    if stats["joker_buys"]:
        w("")
        w("## Top Joker Purchases")
        w("")
        w("| Joker | Bought |")
        w("|-------|--------|")
        for name, count in stats["joker_buys"].most_common(15):
            w(f"| {name} | {count} |")

    # ═══════════════════════════════════════════════════════════════
    # xMult + Utility breakdown
    # ═══════════════════════════════════════════════════════════════
    xmult_total = sum(stats["xmult_buys"].values())
    util_total = sum(stats["utility_buys"].values())
    if xmult_total or util_total:
        w("")
        w("## Joker Categories")
        w("")
        if xmult_total:
            w(f"### xMult Jokers ({xmult_total} bought)")
            w("")
            w("| Joker | Count |")
            w("|-------|-------|")
            for name, count in stats["xmult_buys"].most_common():
                w(f"| {name} | {count} |")
            w("")
        if util_total:
            w(f"### Utility Jokers ({util_total} bought)")
            w("")
            w("| Joker | Count |")
            w("|-------|-------|")
            for name, count in stats["utility_buys"].most_common():
                w(f"| {name} | {count} |")

    # ═══════════════════════════════════════════════════════════════
    # Voucher Purchases
    # ═══════════════════════════════════════════════════════════════
    if stats["voucher_buys"]:
        w("")
        w("## Voucher Purchases")
        w("")
        w("| Voucher | Count |")
        w("|---------|-------|")
        for name, count in stats["voucher_buys"].most_common():
            w(f"| {name} | {count} |")

    # ═══════════════════════════════════════════════════════════════
    # Winning Rosters
    # ═══════════════════════════════════════════════════════════════
    if stats["final_jokers_wins"] and wins >= 1:
        w("")
        w("## Jokers in Winning Rosters")
        w("")
        w(f"| Joker | Appearances | out of {wins} wins |")
        w(f"|-------|-------------|{'---' * 5}|")
        for name, count in stats["final_jokers_wins"].most_common(15):
            w(f"| {name} | {count} | {_pct(count, wins)} |")

    # ═══════════════════════════════════════════════════════════════
    # Misc: synergy, triggered utility
    # ═══════════════════════════════════════════════════════════════
    triggered = stats["luchador_sells"] + stats["invisible_sells"] + stats["diet_cola_sells"]
    if stats["synergy_buys"] or triggered:
        w("")
        w("## Miscellaneous")
        w("")
        if stats["synergy_buys"]:
            w(f"- Cross-synergy boosted purchases: **{stats['synergy_buys']}**")
        if stats["luchador_sells"]:
            w(f"- Luchador boss-cancel sells: **{stats['luchador_sells']}**")
        if stats["invisible_sells"]:
            w(f"- Invisible Joker dupe sells: **{stats['invisible_sells']}**")
        if stats["diet_cola_sells"]:
            w(f"- Diet Cola free reroll sells: **{stats['diet_cola_sells']}**")

    # ═══════════════════════════════════════════════════════════════
    # Scoring Accuracy
    # ═══════════════════════════════════════════════════════════════
    if stats["total_scores"] > 0:
        w("")
        w("## Scoring Accuracy")
        w("")
        rate = 100 * stats["mismatches"] / stats["total_scores"]
        w(f"| Metric | Value |")
        w(f"|--------|-------|")
        w(f"| Hands scored | {stats['total_scores']} |")
        w(f"| Mismatches | {stats['mismatches']} ({rate:.1f}%) |")
        if stats["mismatch_diffs"]:
            diffs = stats["mismatch_diffs"]
            avg_diff = sum(diffs) / len(diffs)
            over = sum(1 for d in diffs if d < 0)
            under = len(diffs) - over
            w(f"| Avg diff | {avg_diff:+,.0f} chips |")
            w(f"| Over-estimates | {over} |")
            w(f"| Under-estimates | {under} |")

    # ═══════════════════════════════════════════════════════════════
    # Milk Actions
    # ═══════════════════════════════════════════════════════════════
    milk_total = sum(stats["milk_actions"].values())
    if milk_total:
        w("")
        w(f"## Milk Actions ({milk_total} total)")
        w("")
        w("| Action | Count |")
        w("|--------|-------|")
        for action, count in stats["milk_actions"].most_common(10):
            w(f"| {action} | {count} |")

    w("")
    return "\n".join(lines)


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


if __name__ == "__main__":
    main()
