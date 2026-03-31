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
RE_JOKER_SELL = re.compile(r"sell\(\{'joker'.*\| sell (.+?) \(value=")
RE_JOKER_SELL_FOR = re.compile(r"\) for (.+?) \(value=")
RE_CONSUMABLE_BUY = re.compile(r"buy consumable: (.+?)(?:\s+for\s+\$\d+)?\s+\(")
RE_VOUCHER_BUY = re.compile(r"buy voucher: (.+?) for \$")
RE_BLIND = re.compile(r"Blind: (.+?) \(need")
RE_MILK_ACTION = re.compile(r"milk: (.+?) \(")
RE_REROLL = re.compile(r"SHOP -> reroll\(\)")
RE_GAME_START = re.compile(r"Started new game: deck=(\w+)")
RE_ROSTER = re.compile(r"\[ANTE \d+\] Roster \(\d+ jokers\): \[(.+)\]")
RE_BEST_AVAILABLE_PLAY = re.compile(r"SELECTING_HAND -> play.+\| best available:")
RE_DESPERATION_CYCLE = re.compile(r"desperation cycle:")
RE_TAROT_USE = re.compile(r"\| use tarot: (.+?) ->|\| use tarot: (.+?) \(")
RE_TAROT_DESPERATE = re.compile(r"\| desperate: (.+?) \(")
RE_ROSTER_SCALING = re.compile(r"(\w[\w\s]+?)\(([+X][\d.]+(?:chips|mult)?)\)")
RE_SHOP_MONEY = re.compile(r"Shop \(\$(\d+)\):")
RE_PACK_PICK = re.compile(r"SMODS_BOOSTER_OPENED -> pack\(\{'card'")
RE_PACK_SKIP = re.compile(r"SMODS_BOOSTER_OPENED -> pack\(\{'skip': True")

PLANET_NAMES = {
    "Mercury", "Venus", "Earth", "Mars", "Jupiter", "Saturn",
    "Uranus", "Neptune", "Pluto", "Planet X", "Ceres", "Eris",
}


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
    planet_buys = Counter()
    tarot_buys = Counter()
    joker_sells = Counter()
    joker_sell_upgrade = Counter()   # sold as part of an upgrade
    joker_sell_proactive = Counter() # sold proactively (no upgrade target)
    final_money = []            # money at game end
    final_jokers_wins = Counter()   # jokers present in winning games
    final_jokers_losses = Counter() # jokers present in losing games
    rerolls = 0
    pack_picks = 0
    pack_skips = 0
    # Desperation tracking — per round flags, resolved on [ROUND] line
    cur_round_had_desperation = False
    cur_round_had_tarot = False
    cur_round_had_desperate_tarot = False
    desperation_rounds_won = 0
    desperation_rounds_lost = 0
    tarot_rounds_won = 0
    tarot_rounds_lost = 0
    desperate_tarot_rounds_won = 0
    desperate_tarot_rounds_lost = 0
    shop_entries: list[int] = []
    scaling_joker_vals_win: dict[str, list] = defaultdict(list)
    scaling_joker_vals_loss: dict[str, list] = defaultdict(list)
    joker_buy_antes: dict[str, list] = defaultdict(list)  # first buy ante per game

    # Tracking state across lines
    last_round_result = None    # last [ROUND]...LOST line's blind name
    last_hand_type = None       # track last played hand type for round-ending plays
    cur_ante = 0                # current ante number for round tracking
    cur_deck = "UNKNOWN"        # deck type for current game
    cur_roster_line = ""        # last seen Roster line, parsed on game over
    jokers_bought_this_game: set[str] = set()
    deck_stats: dict[str, list] = defaultdict(list)
    hand_types_by_ante: dict[int, Counter] = defaultdict(Counter)

    for line in text.splitlines():
        # Game start — capture deck type
        gs = RE_GAME_START.search(line)
        if gs:
            cur_deck = gs.group(1)

        # Ante tracking
        if "[ANTE " in line and "Roster" in line:
            am = re.search(r"\[ANTE (\d+)\]", line)
            if am:
                cur_ante = int(am.group(1))

        # Blind announcement (track ante for rounds that happen before first ANTE line)
        bm_line = RE_BLIND.search(line)
        if bm_line and "Blind:" in line:
            blind_name = bm_line.group(1)

        # Desperation and tarot signals (per-round flags)
        if RE_BEST_AVAILABLE_PLAY.search(line) or RE_DESPERATION_CYCLE.search(line):
            cur_round_had_desperation = True
        tm = RE_TAROT_USE.search(line)
        if tm:
            cur_round_had_tarot = True
        dtm = RE_TAROT_DESPERATE.search(line)
        if dtm:
            cur_round_had_desperate_tarot = True

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

            if cur_round_had_desperation:
                if won:
                    desperation_rounds_won += 1
                else:
                    desperation_rounds_lost += 1
            if cur_round_had_tarot:
                if won:
                    tarot_rounds_won += 1
                else:
                    tarot_rounds_lost += 1
            if cur_round_had_desperate_tarot:
                if won:
                    desperate_tarot_rounds_won += 1
                else:
                    desperate_tarot_rounds_lost += 1
            cur_round_had_desperation = False
            cur_round_had_tarot = False
            cur_round_had_desperate_tarot = False

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
            # Milk plays are for scaling, not scoring — exclude from hand type stats
            elif hand_type.startswith("milk"):
                continue
            # Handle "mouth locked (X) but can't form it: playing Type for N"
            elif "mouth locked" in hand_type:
                mlk = RE_MOUTH_LOCKED.search(line)
                if mlk:
                    hand_type = mlk.group(1)
            hand_type = hand_type.strip()
            if hand_type in KNOWN_HAND_TYPES:
                hand_types_played[hand_type] += 1
                hand_types_by_ante[cur_ante][hand_type] += 1
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
            if not is_win or "died in endless" in line:
                ante_deaths[ante] += 1
                if last_round_result:
                    killing_blinds[last_round_result] += 1
            deck_stats[cur_deck].append({"ante": ante, "win": is_win})
            # Parse scaling joker values from last roster line
            if cur_roster_line and cur_roster_line != "[none]":
                target = scaling_joker_vals_win if is_win else scaling_joker_vals_loss
                for jname, val_str in RE_ROSTER_SCALING.findall(cur_roster_line):
                    jname = jname.strip()
                    try:
                        val = float(val_str.lstrip("+X").replace("chips", "").replace("mult", ""))
                        target[jname].append(val)
                    except ValueError:
                        pass
            last_round_result = None
            last_hand_type = None
            cur_ante = 0
            cur_deck = "UNKNOWN"
            cur_roster_line = ""
            jokers_bought_this_game = set()

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
            # First buy ante tracking
            if name not in jokers_bought_this_game:
                jokers_bought_this_game.add(name)
                joker_buy_antes[name].append(cur_ante)

        # Joker sells
        jsm = RE_JOKER_SELL.search(line)
        if jsm:
            name = jsm.group(1).strip()
            joker_sells[name] += 1
            if RE_JOKER_SELL_FOR.search(line):
                joker_sell_upgrade[name] += 1
            else:
                joker_sell_proactive[name] += 1

        # Consumable purchases (planets + tarots/spectrals)
        cm = RE_CONSUMABLE_BUY.search(line)
        if cm:
            cname = cm.group(1).strip()
            if cname in PLANET_NAMES:
                planet_buys[cname] += 1
            else:
                tarot_buys[cname] += 1

        # Voucher purchases
        vm = RE_VOUCHER_BUY.search(line)
        if vm:
            voucher_buys[vm.group(1)] += 1

        # Shop entry money
        shm = RE_SHOP_MONEY.search(line)
        if shm:
            shop_entries.append(int(shm.group(1)))

        # Pack picks and skips
        if RE_PACK_PICK.search(line):
            pack_picks += 1
        elif RE_PACK_SKIP.search(line):
            pack_skips += 1

        # Roster line (keep latest — parsed on game over)
        rm2 = RE_ROSTER.search(line)
        if rm2:
            cur_roster_line = rm2.group(1)

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
        "planet_buys": planet_buys,
        "tarot_buys": tarot_buys,
        "joker_sells": joker_sells,
        "joker_sell_upgrade": joker_sell_upgrade,
        "joker_sell_proactive": joker_sell_proactive,
        "final_money": final_money,
        "final_jokers_wins": final_jokers_wins,
        "final_jokers_losses": final_jokers_losses,
        "rerolls": rerolls,
        "pack_picks": pack_picks,
        "pack_skips": pack_skips,
        "desperation_rounds_won": desperation_rounds_won,
        "desperation_rounds_lost": desperation_rounds_lost,
        "tarot_rounds_won": tarot_rounds_won,
        "tarot_rounds_lost": tarot_rounds_lost,
        "desperate_tarot_rounds_won": desperate_tarot_rounds_won,
        "desperate_tarot_rounds_lost": desperate_tarot_rounds_lost,
        "shop_entries": shop_entries,
        "scaling_joker_vals_win": dict(scaling_joker_vals_win),
        "scaling_joker_vals_loss": dict(scaling_joker_vals_loss),
        "joker_buy_antes": dict(joker_buy_antes),
        "deck_stats": dict(deck_stats),
        "hand_types_by_ante": dict(hand_types_by_ante),
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
        "planet_buys": Counter(),
        "tarot_buys": Counter(),
        "joker_sells": Counter(),
        "joker_sell_upgrade": Counter(),
        "joker_sell_proactive": Counter(),
        "final_money": [],
        "final_jokers_wins": Counter(),
        "final_jokers_losses": Counter(),
        "rerolls": 0,
        "pack_picks": 0,
        "pack_skips": 0,
        "desperation_rounds_won": 0,
        "desperation_rounds_lost": 0,
        "tarot_rounds_won": 0,
        "tarot_rounds_lost": 0,
        "desperate_tarot_rounds_won": 0,
        "desperate_tarot_rounds_lost": 0,
        "shop_entries": [],
        "scaling_joker_vals_win": defaultdict(list),
        "scaling_joker_vals_loss": defaultdict(list),
        "joker_buy_antes": defaultdict(list),
        "deck_stats": defaultdict(list),
        "hand_types_by_ante": defaultdict(Counter),
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
        merged["planet_buys"] += s.get("planet_buys", Counter())
        merged["tarot_buys"] += s.get("tarot_buys", Counter())
        merged["joker_sells"] += s.get("joker_sells", Counter())
        merged["joker_sell_upgrade"] += s.get("joker_sell_upgrade", Counter())
        merged["joker_sell_proactive"] += s.get("joker_sell_proactive", Counter())
        merged["final_money"].extend(s.get("final_money", []))
        merged["final_jokers_wins"] += s.get("final_jokers_wins", Counter())
        merged["final_jokers_losses"] += s.get("final_jokers_losses", Counter())
        merged["rerolls"] += s.get("rerolls", 0)
        merged["pack_picks"] += s.get("pack_picks", 0)
        merged["pack_skips"] += s.get("pack_skips", 0)
        merged["desperation_rounds_won"] += s.get("desperation_rounds_won", 0)
        merged["desperation_rounds_lost"] += s.get("desperation_rounds_lost", 0)
        merged["tarot_rounds_won"] += s.get("tarot_rounds_won", 0)
        merged["tarot_rounds_lost"] += s.get("tarot_rounds_lost", 0)
        merged["desperate_tarot_rounds_won"] += s.get("desperate_tarot_rounds_won", 0)
        merged["desperate_tarot_rounds_lost"] += s.get("desperate_tarot_rounds_lost", 0)
        merged["shop_entries"].extend(s.get("shop_entries", []))
        for jname, vals in s.get("scaling_joker_vals_win", {}).items():
            merged["scaling_joker_vals_win"][jname].extend(vals)
        for jname, vals in s.get("scaling_joker_vals_loss", {}).items():
            merged["scaling_joker_vals_loss"][jname].extend(vals)
        for jname, antes in s.get("joker_buy_antes", {}).items():
            merged["joker_buy_antes"][jname].extend(antes)
        for deck, entries in s.get("deck_stats", {}).items():
            merged["deck_stats"][deck].extend(entries)
        for ante, counter in s.get("hand_types_by_ante", {}).items():
            merged["hand_types_by_ante"][ante] += counter
        merged["total_scores"] += s.get("total_scores", 0)
        merged["mismatches"] += s.get("mismatches", 0)
        merged["mismatch_diffs"].extend(s.get("mismatch_diffs", []))
    return merged


def _md_table(headers: list, rows: list, right_cols: set | None = None) -> list[str]:
    """Return padded markdown table lines with uniform column widths.
    right_cols: set of column indices to right-justify (default: all except col 0).
    """
    if right_cols is None:
        right_cols = set(range(1, len(headers)))
    str_rows = [[str(c) for c in row] for row in rows]
    widths = []
    for i in range(len(headers)):
        col_vals = [len(str(headers[i]))] + [len(r[i]) for r in str_rows]
        widths.append(max(col_vals))
    out = []
    out.append("| " + " | ".join(str(h).ljust(widths[i]) for i, h in enumerate(headers)) + " |")
    out.append("|" + "|".join("-" * (w + 2) for w in widths) + "|")
    for row in str_rows:
        cells = [
            row[i].rjust(widths[i]) if i in right_cols else row[i].ljust(widths[i])
            for i in range(len(row))
        ]
        out.append("| " + " | ".join(cells) + " |")
    return out


def _bar(n: int, total: int, width: int = 20) -> str:
    """Unicode bar chart segment."""
    if total == 0:
        return ""
    filled = max(1, round(width * n / total))
    return "\u2588" * filled + "\u2591" * (width - filled)


def _pct(n: int, total: int, width: int = 0) -> str:
    if total == 0:
        result = "0.0%"
    else:
        result = f"{100 * n / total:.1f}%"
    return result.rjust(width) if width else result


def generate_markdown(stats: dict, batch_label: str) -> str:
    """Generate a markdown report from parsed stats."""
    lines: list[str] = []
    w = lines.append  # shorthand

    def table(headers, rows, right_cols=None):
        for line in _md_table(headers, rows, right_cols):
            w(line)

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
    header_rows = [
        ["Games", str(total)],
        ["Wins", f"{wins} ({_pct(wins, total)})"],
        ["Avg Ante", f"{avg_ante:.1f}"],
        ["Median Ante", str(median_ante)],
        ["Max Ante", str(max_ante)],
    ]
    if stats["final_money"]:
        avg_money = sum(stats["final_money"]) / len(stats["final_money"])
        med_money = sorted(stats["final_money"])[len(stats["final_money"]) // 2]
        header_rows.append(["Avg Final $", f"${avg_money:.0f}"])
        header_rows.append(["Median Final $", f"${med_money}"])
    header_rows.append(["Total Rounds", str(len(rounds))])
    tarot_total = stats.get("tarot_rounds_won", 0) + stats.get("tarot_rounds_lost", 0)
    if tarot_total:
        header_rows.append(["Tarots Used", str(tarot_total)])
    header_rows.append(["Rerolls", str(stats["rerolls"])])
    table(["Stat", "Value"], header_rows, right_cols={1})

    # ═══════════════════════════════════════════════════════════════
    # RUN OVERVIEW
    # ═══════════════════════════════════════════════════════════════
    if stats["ante_deaths"]:
        w("")
        w("## Deaths by Ante")
        w("")
        surviving = total
        death_rows = []
        for ante in sorted(stats["ante_deaths"]):
            count = stats["ante_deaths"][ante]
            bar = f"`{_bar(surviving, total)}`"
            death_rows.append([str(ante), str(count), _pct(count, total), _pct(surviving, total), bar])
            surviving -= count
        table(["Ante", "Deaths", "Death %", "Reached %", ""], death_rows, right_cols={0, 1, 2, 3})

    deck_stats = stats.get("deck_stats", {})
    if deck_stats:
        w("")
        w("## Deck Breakdown")
        w("")
        deck_rows = []
        for deck, entries in sorted(deck_stats.items(), key=lambda x: -len(x[1])):
            d_total = len(entries)
            d_wins = sum(1 for e in entries if e["win"])
            d_antes = [e["ante"] for e in entries]
            d_avg = sum(d_antes) / len(d_antes)
            d_med = sorted(d_antes)[len(d_antes) // 2]
            deck_rows.append([deck, str(d_total), str(d_wins), _pct(d_wins, d_total), f"{d_avg:.1f}", str(d_med)])
        table(["Deck", "Games", "Wins", "Win%", "Avg Ante", "Median Ante"], deck_rows, right_cols={1, 2, 3, 4, 5})

    # ═══════════════════════════════════════════════════════════════
    # BLIND PERFORMANCE
    # ═══════════════════════════════════════════════════════════════
    if stats["killing_blinds"]:
        w("")
        w("## Killing Blind")
        w("")
        w("What blind type ended each lost run.")
        w("")
        small = stats["killing_blinds"].get("Small Blind", 0)
        big = stats["killing_blinds"].get("Big Blind", 0)
        boss = sum(c for name, c in stats["killing_blinds"].items() if name not in ("Small Blind", "Big Blind"))
        kb_rows = [
            ["Small Blind", str(small), _pct(small, losses)],
            ["Big Blind",   str(big),   _pct(big, losses)],
            ["Boss Blind",  str(boss),  _pct(boss, losses)],
        ]
        table(["Blind", "Deaths", "% of Losses"], kb_rows, right_cols={1, 2})

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
        sorted_bosses = sorted(boss_stats.items(), key=lambda x: x[1]["wins"] + x[1]["losses"], reverse=True)
        boss_rows = []
        for name, bs in sorted_bosses:
            t = bs["wins"] + bs["losses"]
            wr = 100 * bs["wins"] / t if t > 0 else 0
            flag = " :warning:" if wr < 60 else ""
            boss_rows.append([name, str(bs["wins"]), str(bs["losses"]), str(t), f"{wr:.0f}%{flag}"])
        table(["Boss", "W", "L", "Total", "Win%"], boss_rows, right_cols={1, 2, 3, 4})

        bands = [("1–3", lambda a: 1 <= a <= 3), ("4–6", lambda a: 4 <= a <= 6), ("7+", lambda a: a >= 7)]
        for band_label, pred in bands:
            band_rounds = [r for r in boss_rounds if pred(r["ante"])]
            if not band_rounds:
                continue
            band_stats: dict[str, dict] = {}
            for r in band_rounds:
                n = r["blind"]
                if n not in band_stats:
                    band_stats[n] = {"wins": 0, "losses": 0}
                if r["won"]:
                    band_stats[n]["wins"] += 1
                else:
                    band_stats[n]["losses"] += 1
            sorted_band = sorted(band_stats.items(), key=lambda x: x[1]["wins"] + x[1]["losses"], reverse=True)
            band_rows = []
            for n, bs in sorted_band:
                t = bs["wins"] + bs["losses"]
                wr = 100 * bs["wins"] / t if t > 0 else 0
                flag = " :warning:" if wr < 60 else ""
                band_rows.append([n, str(bs["wins"]), str(bs["losses"]), str(t), f"{wr:.0f}%{flag}"])
            w("")
            w(f"### Antes {band_label}")
            w("")
            table(["Boss", "W", "L", "Total", "Win%"], band_rows, right_cols={1, 2, 3, 4})

    won_rounds = [r for r in rounds if r["won"]]
    if won_rounds:
        close_by_blind: dict[str, dict] = {}
        for r in won_rounds:
            if r["needed"] > 0:
                b = r["blind"]
                if b not in close_by_blind:
                    close_by_blind[b] = {"close": 0, "total": 0}
                close_by_blind[b]["total"] += 1
                if r["scored"] / r["needed"] < 1.15:
                    close_by_blind[b]["close"] += 1
        close_blinds = [(b, d) for b, d in close_by_blind.items() if d["close"] > 0]
        if close_blinds:
            w("")
            w("## Close Calls by Blind")
            w("")
            close_blinds.sort(key=lambda x: -x[1]["close"])
            cb_rows = [[b, str(d["close"]), str(d["total"]), _pct(d["close"], d["total"])]
                       for b, d in close_blinds[:15]]
            table(["Blind", "Close Wins", "Total Wins", "Close %"], cb_rows, right_cols={1, 2, 3})

    # ═══════════════════════════════════════════════════════════════
    # ROUND EFFICIENCY
    # ═══════════════════════════════════════════════════════════════
    if won_rounds:
        w("")
        w("## Round Efficiency")
        w("")
        one_shots = sum(1 for r in won_rounds if r["hands"] == 1)
        avg_hands = sum(r["hands"] for r in won_rounds) / len(won_rounds)
        close = [r for r in won_rounds if r["needed"] > 0 and r["scored"] / r["needed"] < 1.15]
        shop_ents = stats.get("shop_entries", [])
        over_cap = sum(1 for m in shop_ents if m >= 30)
        pp = stats.get("pack_picks", 0)
        ps = stats.get("pack_skips", 0)
        eff_rows = [
            ["One-shot rate", f"{one_shots}/{len(won_rounds)} ({_pct(one_shots, len(won_rounds))})"],
            ["Avg hands/round", f"{avg_hands:.1f}"],
            ["Close calls (<15% margin)", f"{len(close)} ({_pct(len(close), len(won_rounds))})"],
        ]
        if shop_ents:
            eff_rows.append(["Over interest cap ($30+)", f"{over_cap}/{len(shop_ents)} ({_pct(over_cap, len(shop_ents))})"])
        if pp + ps > 0:
            eff_rows.append(["Pack pick rate", f"{pp} picks / {ps} skips ({_pct(pp, pp + ps)})"])
        table(["Metric", "Value"], eff_rows, right_cols={1})

        dw = stats.get("desperation_rounds_won", 0)
        dl = stats.get("desperation_rounds_lost", 0)
        tw = stats.get("tarot_rounds_won", 0)
        tl = stats.get("tarot_rounds_lost", 0)
        dtw = stats.get("desperate_tarot_rounds_won", 0)
        dtl = stats.get("desperate_tarot_rounds_lost", 0)
        if dw + dl > 0 or tw + tl > 0:
            w("")
            w("### Desperation & Tarot Survival")
            w("")
            surv_rows = []
            if dw + dl > 0:
                surv_rows.append(["Desperation plays", f"{dw}W / {dl}L", _pct(dw, dw + dl)])
            if dtw + dtl > 0:
                surv_rows.append(["Tarot used (desperate)", f"{dtw}W / {dtl}L", _pct(dtw, dtw + dtl)])
            table(["Scenario", "Outcome", "Win%"], surv_rows, right_cols={1, 2})

        ante_overkill: dict[int, list] = defaultdict(list)
        ante_hands_won: dict[int, list] = defaultdict(list)
        for r in won_rounds:
            if r["needed"] > 0 and r["ante"] > 0:
                ante_overkill[r["ante"]].append(r["scored"] / r["needed"])
            if r["ante"] > 0:
                ante_hands_won[r["ante"]].append(r["hands"])
        if ante_overkill:
            w("")
            w("### Scoring Headroom by Ante")
            w("")
            w("Average (scored / needed) and hands used for won rounds.")
            w("")
            ok_rows = []
            for ante in sorted(ante_overkill):
                vals = ante_overkill[ante]
                avg_ok = sum(vals) / len(vals)
                avg_h = sum(ante_hands_won[ante]) / len(ante_hands_won[ante]) if ante_hands_won[ante] else 0
                ok_rows.append([str(ante), f"{avg_ok:.1f}x", f"{avg_h:.1f}", str(len(vals))])
            table(["Ante", "Overkill", "Avg Hands", "Rounds"], ok_rows, right_cols={0, 1, 2, 3})

    if stats["hand_types_played"]:
        w("")
        w("## Hand Types Played")
        w("")
        total_plays = sum(stats["hand_types_played"].values())
        ht_rows = [[ht, str(count), _pct(count, total_plays)]
                   for ht, count in stats["hand_types_played"].most_common()]
        table(["Hand Type", "Count", "%"], ht_rows, right_cols={1, 2})

    htba = stats.get("hand_types_by_ante", {})
    if htba:
        bands = [("Antes 1–3", range(1, 4)), ("Antes 4–6", range(4, 7)), ("Antes 7+", range(7, 20))]
        w("")
        w("## Hand Types by Ante Band")
        for band_label, band_range in bands:
            band_counter: Counter = Counter()
            for ante in band_range:
                band_counter += htba.get(ante, Counter())
            if not band_counter:
                continue
            band_total = sum(band_counter.values())
            w("")
            w(f"### {band_label}")
            w("")
            band_rows = [[ht, str(count), _pct(count, band_total)]
                         for ht, count in band_counter.most_common()]
            table(["Hand Type", "Count", "%"], band_rows, right_cols={1, 2})

    # ═══════════════════════════════════════════════════════════════
    # ECONOMY
    # ═══════════════════════════════════════════════════════════════
    if stats.get("planet_buys") or stats.get("tarot_buys"):
        w("")
        w("## Consumable Purchases")
        if stats.get("planet_buys"):
            w("")
            w("### Planets")
            w("")
            planet_rows = [[name, str(count)] for name, count in stats["planet_buys"].most_common()]
            table(["Planet", "Count"], planet_rows, right_cols={1})
        if stats.get("tarot_buys"):
            w("")
            w("### Tarots & Spectrals")
            w("")
            tarot_rows = [[name, str(count)] for name, count in stats["tarot_buys"].most_common(15)]
            table(["Card", "Count"], tarot_rows, right_cols={1})

    if stats["voucher_buys"]:
        w("")
        w("## Voucher Purchases")
        w("")
        vb_rows = [[name, str(count)] for name, count in stats["voucher_buys"].most_common()]
        table(["Voucher", "Count"], vb_rows, right_cols={1})

    xmult_total = sum(stats["xmult_buys"].values())
    util_total = sum(stats["utility_buys"].values())
    if stats["joker_buys"] or xmult_total or util_total:
        w("")
        w("## Joker Categories")
        w("")
        if stats["joker_buys"]:
            w("### All Purchases")
            w("")
            jb_rows = [[name, str(count)] for name, count in stats["joker_buys"].most_common(15)]
            table(["Joker", "Bought"], jb_rows, right_cols={1})
            w("")
        if xmult_total:
            w(f"### xMult Jokers ({xmult_total} bought)")
            w("")
            xm_rows = [[name, str(count)] for name, count in stats["xmult_buys"].most_common()]
            table(["Joker", "Count"], xm_rows, right_cols={1})
            w("")
        if util_total:
            w(f"### Utility Jokers ({util_total} bought)")
            w("")
            ut_rows = [[name, str(count)] for name, count in stats["utility_buys"].most_common()]
            table(["Joker", "Count"], ut_rows, right_cols={1})

    joker_buy_antes = stats.get("joker_buy_antes", {})
    if joker_buy_antes:
        w("")
        w("## Joker Acquisition Timing")
        w("")
        w("Average ante at first acquisition (top jokers by purchase count).")
        w("")
        jb = stats["joker_buys"]
        timing_rows = []
        for name, _ in jb.most_common(15):
            antes = joker_buy_antes.get(name, [])
            if antes:
                avg_ante = sum(antes) / len(antes)
                timing_rows.append([name, f"{avg_ante:.1f}", str(len(antes))])
        if timing_rows:
            table(["Joker", "Avg Ante", "Count"], timing_rows, right_cols={1, 2})

    if stats.get("joker_sells"):
        w("")
        w("## Joker Sells")
        w("")
        sell_rows = []
        for name, count in stats["joker_sells"].most_common(15):
            up = stats["joker_sell_upgrade"].get(name, 0)
            pro = stats["joker_sell_proactive"].get(name, 0)
            sell_rows.append([name, str(count), str(up), str(pro)])
        table(["Joker", "Sold", "Upgrade", "Proactive"], sell_rows, right_cols={1, 2, 3})

    # ═══════════════════════════════════════════════════════════════
    # JOKERS
    # ═══════════════════════════════════════════════════════════════
    win_vals = stats.get("scaling_joker_vals_win", {})
    loss_vals = stats.get("scaling_joker_vals_loss", {})
    all_scaling_names = set(win_vals) | set(loss_vals)
    if all_scaling_names:
        w("")
        w("## Scaling Joker Values at End of Game")
        w("")
        w("Average accumulated value from last Roster line, split by outcome.")
        w("")
        scaling_rows = []
        for name in sorted(all_scaling_names, key=lambda n: -(len(win_vals.get(n, [])) + len(loss_vals.get(n, [])))):
            wv = win_vals.get(name, [])
            lv = loss_vals.get(name, [])
            avg_w = f"{sum(wv)/len(wv):.1f}" if wv else "—"
            avg_l = f"{sum(lv)/len(lv):.1f}" if lv else "—"
            scaling_rows.append([name, avg_w, avg_l, str(len(wv) + len(lv))])
        table(["Joker", "Avg (Wins)", "Avg (Losses)", "Games"], scaling_rows, right_cols={1, 2, 3})

    if stats["final_jokers_wins"] and wins >= 1:
        w("")
        w("## Jokers in Winning Rosters")
        w("")
        wr_rows = [[name, str(count), _pct(count, wins)]
                   for name, count in stats["final_jokers_wins"].most_common()]
        table(["Joker", "Appearances", f"out of {wins} wins"], wr_rows, right_cols={1, 2})

    # ═══════════════════════════════════════════════════════════════
    # DIAGNOSTICS
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

    if stats["total_scores"] > 0:
        w("")
        w("## Scoring Accuracy")
        w("")
        rate = 100 * stats["mismatches"] / stats["total_scores"]
        acc_rows = [
            ["Hands scored", str(stats["total_scores"])],
            ["Mismatches", f"{stats['mismatches']} ({rate:.1f}%)"],
        ]
        if stats["mismatch_diffs"]:
            diffs = stats["mismatch_diffs"]
            avg_diff = sum(diffs) / len(diffs)
            over = sum(1 for d in diffs if d < 0)
            under = len(diffs) - over
            acc_rows.append(["Avg diff", f"{avg_diff:+,.0f} chips"])
            acc_rows.append(["Over-estimates", str(over)])
            acc_rows.append(["Under-estimates", str(under)])
        table(["Metric", "Value"], acc_rows, right_cols={1})

    milk_total = sum(stats["milk_actions"].values())
    if milk_total:
        w("")
        w(f"## Milk Actions ({milk_total} total)")
        w("")
        milk_rows = [[action, str(count)] for action, count in stats["milk_actions"].most_common(10)]
        table(["Action", "Count"], milk_rows, right_cols={1})

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
