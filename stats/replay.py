from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

from .parser import _parse_comma_int


def find_winning_games(log_path: Path) -> list[list[str]]:
    """Split log into per-game chunks and return only winning ones."""
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    games: list[list[str]] = []
    cur: list[str] = []
    for line in lines:
        if "=== Game " in line and "/" in line:
            if cur:
                games.append(cur)
            cur = [line]
        else:
            cur.append(line)
    if cur:
        games.append(cur)
    return [g for g in games if any("VICTORY at ante" in l for l in g)]


def parse_win_game(lines: list[str]) -> dict:
    """Parse a single winning game's log lines into structured data."""
    # Local regexes
    _RE_START   = re.compile(r"Started new game: deck=(\w+) stake=(\w+) seed=(\w+)")
    _RE_SUMMARY = re.compile(r"Summary: \$(\d+) \| jokers: \[(.+)\] \| hands=(\d+) discards=(\d+)")
    _RE_VICTORY = re.compile(r"VICTORY at ante (\d+)")
    _RE_BLIND   = re.compile(r"Blind: (.+?) \(need ([\d,]+) chips\)")
    _RE_HAND    = re.compile(r"Hand: \[(.+?)\](?:.*\| DEBUFFED: \[(.+?)\])?")
    _RE_BEST    = re.compile(r"\[HAND\] Best: (.+?) for ([\d,]+).*(CAN WIN|NEED MORE)")
    _RE_PLAY    = re.compile(r"SELECTING_HAND -> play\(.*?\) \[(.+?)\] \| (.+?) for ([\d,]+)")
    _RE_DISCARD = re.compile(r"SELECTING_HAND -> discard\(.*?\) \[(.+?)\] \| (.+)")
    _RE_CHASE   = re.compile(r"chase (.+?) \((\d+)% to hit\).*\[([\d.]+)x\]")
    _RE_ROUND   = re.compile(r"\[ROUND\] (.+?): scored ([\d,]+) / needed ([\d,]+) — (WON|LOST) \| (\d+) hands?, (\d+) discards?")
    _RE_USE     = re.compile(r"SELECTING_HAND -> use\(.*?\) \| (.+)")
    _RE_BUY_J   = re.compile(r"SHOP -> buy\(.*?\) \| buy joker: (.+?) for \$(\d+)")
    _RE_BUY_C   = re.compile(r"SHOP -> buy\(.*?\) \| buy consumable: (.+?) for \$(\d+)")
    _RE_BUY_P   = re.compile(r"SHOP -> buy\(.*?\) \| buy pack: (.+?) for \$(\d+)")
    _RE_BUY_V   = re.compile(r"SHOP -> buy\(.*?\) \| buy voucher: (.+?) for \$(\d+)")
    _RE_SELL    = re.compile(r"SHOP -> sell\(.*?\) \| (.+)")
    _RE_PLANET  = re.compile(r"SMODS_BOOSTER_OPENED -> pack\(.*?\) \| planet: (.+?) \(levels (.+?),")
    _RE_TAROT   = re.compile(r"SMODS_BOOSTER_OPENED -> pack\(.*?\) \| tarot: (.+?) \(value=")
    _RE_ROSTER  = re.compile(r"\[ANTE (\d+)\] Roster \(\d+ jokers\): \[(.+)\]")
    _RE_DECK    = re.compile(r"\[ANTE (\d+)\] Deck (.+)")
    _RE_STRAT   = re.compile(r"\[ANTE (\d+)\] Strategy: (.+)")
    _RE_MONEY   = re.compile(r"\[ANTE (\d+)\] Money: \$(\d+)(?:\s*\|\s*Levels: (.+))?")

    result: dict = {
        "seed": "", "deck": "", "stake": "",
        "final_ante": 0, "final_money": 0,
        "final_jokers": "", "total_hands": 0, "total_discards": 0,
        "antes": [],
    }

    # Ante metadata — updated on each [ANTE N] line (keeps latest per ante_num)
    ante_meta: dict[int, dict] = {}
    # Ordered list of (ante_num, section_index) as ante headers appear
    ante_order: list[int] = []

    rounds: list[dict] = []
    cur_round: dict | None = None
    cur_ante = 0
    in_shop = False
    got_initial_hand = False

    def _new_ante_meta(an: int) -> dict:
        return {"roster": "none", "deck": "", "strategy": "no preference", "money": 0, "levels": ""}

    for line in lines:
        # ── Game start ──────────────────────────────────────────────
        m = _RE_START.search(line)
        if m:
            result["deck"] = m.group(1)
            result["stake"] = m.group(2)
            result["seed"] = m.group(3)
            continue

        # ── Summary ─────────────────────────────────────────────────
        m = _RE_SUMMARY.search(line)
        if m:
            result["final_money"] = int(m.group(1))
            result["final_jokers"] = m.group(2)
            result["total_hands"] = int(m.group(3))
            result["total_discards"] = int(m.group(4))
            continue

        # ── Victory ─────────────────────────────────────────────────
        m = _RE_VICTORY.search(line)
        if m:
            result["final_ante"] = int(m.group(1))
            continue

        # ── Ante metadata ────────────────────────────────────────────
        m = _RE_ROSTER.search(line)
        if m:
            an = int(m.group(1))
            cur_ante = an
            if an not in ante_meta:
                ante_meta[an] = _new_ante_meta(an)
                ante_order.append(an)
            ante_meta[an]["roster"] = m.group(2)
            continue

        m = _RE_DECK.search(line)
        if m:
            an = int(m.group(1))
            if an not in ante_meta:
                ante_meta[an] = _new_ante_meta(an)
            ante_meta[an]["deck"] = m.group(2)
            continue

        m = _RE_STRAT.search(line)
        if m:
            an = int(m.group(1))
            if an not in ante_meta:
                ante_meta[an] = _new_ante_meta(an)
            ante_meta[an]["strategy"] = m.group(2)
            continue

        m = _RE_MONEY.search(line)
        if m:
            an = int(m.group(1))
            if an not in ante_meta:
                ante_meta[an] = _new_ante_meta(an)
            ante_meta[an]["money"] = int(m.group(2))
            ante_meta[an]["levels"] = m.group(3) or ""
            continue

        # ── Blind announcement → new round ───────────────────────────
        m = _RE_BLIND.search(line)
        if m and "Blind:" in line and "SELECTING_HAND" not in line:
            blind_name = m.group(1)
            needed = _parse_comma_int(m.group(2))
            is_boss = blind_name not in ("Small Blind", "Big Blind")
            cur_round = {
                "blind": blind_name,
                "needed": needed,
                "is_boss": is_boss,
                "ante": cur_ante,
                "initial_hand": "",
                "initial_debuffed": "",
                "best_hand": "",
                "best_score": 0,
                "can_win": False,
                "actions": [],
                "shop": {"buys": [], "sells": [], "packs": []},
                "scored": 0,
                "won": False,
                "hands_used": 0,
                "discards_used": 0,
            }
            got_initial_hand = False
            in_shop = False
            continue

        # ── Initial hand (first Hand: line per round) ─────────────────
        m = _RE_HAND.search(line)
        if m and "Hand:" in line and "SELECTING_HAND" not in line and cur_round is not None:
            if not got_initial_hand:
                cur_round["initial_hand"] = m.group(1)
                cur_round["initial_debuffed"] = m.group(2) or ""
                got_initial_hand = True
            continue

        # ── [HAND] Best ───────────────────────────────────────────────
        m = _RE_BEST.search(line)
        if m and cur_round is not None:
            if not got_initial_hand:
                # edge case: best seen before any Hand: line
                pass
            # Only capture the first [HAND] Best (initial assessment)
            if not cur_round["best_hand"]:
                cur_round["best_hand"] = m.group(1)
                cur_round["best_score"] = _parse_comma_int(m.group(2))
                cur_round["can_win"] = (m.group(3) == "CAN WIN")
            continue

        # ── SELECTING_HAND actions ────────────────────────────────────
        if "SELECTING_HAND ->" in line and cur_round is not None:
            # Play
            m = _RE_PLAY.search(line)
            if m:
                cards = m.group(1)
                hand_type = m.group(2).strip()
                # Normalise special formats
                if hand_type.startswith("best available"):
                    ba = re.search(r"best available: (.+?) for", line)
                    if ba:
                        hand_type = ba.group(1).strip()
                elif "mouth locked" in hand_type:
                    ml = re.search(r"playing (.+?) for", line)
                    if ml:
                        hand_type = ml.group(1).strip()
                score = _parse_comma_int(m.group(3))
                cur_round["actions"].append({
                    "type": "play",
                    "cards": cards,
                    "hand_type": hand_type,
                    "score": score,
                })
                continue

            # Discard
            m = _RE_DISCARD.search(line)
            if m:
                cards = m.group(1)
                reason = m.group(2)
                chase_m = _RE_CHASE.search(reason)
                action: dict = {
                    "type": "discard",
                    "cards": cards,
                    "reason": reason,
                    "chase_hand": "",
                    "chase_pct": 0,
                    "chase_ratio": 0.0,
                }
                if chase_m:
                    action["chase_hand"] = chase_m.group(1)
                    action["chase_pct"] = int(chase_m.group(2))
                    action["chase_ratio"] = float(chase_m.group(3))
                cur_round["actions"].append(action)
                continue

            # Use consumable
            m = _RE_USE.search(line)
            if m:
                cur_round["actions"].append({"type": "use", "detail": m.group(1)})
                continue

        # ── Cash-out → shop phase begins ──────────────────────────────
        if "ROUND_EVAL -> cash_out()" in line:
            in_shop = True
            continue

        # ── Shop actions ──────────────────────────────────────────────
        if in_shop and cur_round is not None:
            m = _RE_BUY_J.search(line)
            if m:
                cur_round["shop"]["buys"].append(
                    {"type": "joker", "name": m.group(1), "cost": int(m.group(2))})
                continue
            m = _RE_BUY_C.search(line)
            if m:
                cur_round["shop"]["buys"].append(
                    {"type": "consumable", "name": m.group(1), "cost": int(m.group(2))})
                continue
            m = _RE_BUY_P.search(line)
            if m:
                cur_round["shop"]["buys"].append(
                    {"type": "pack", "name": m.group(1), "cost": int(m.group(2))})
                continue
            m = _RE_BUY_V.search(line)
            if m:
                cur_round["shop"]["buys"].append(
                    {"type": "voucher", "name": m.group(1), "cost": int(m.group(2))})
                continue
            m = _RE_SELL.search(line)
            if m:
                cur_round["shop"]["sells"].append(m.group(1))
                continue
            m = _RE_PLANET.search(line)
            if m:
                cur_round["shop"]["packs"].append(
                    {"type": "planet", "name": m.group(1), "levels": m.group(2)})
                continue
            m = _RE_TAROT.search(line)
            if m:
                cur_round["shop"]["packs"].append({"type": "tarot", "name": m.group(1)})
                continue

        # ── Round result (logged after next_round) ────────────────────
        m = _RE_ROUND.search(line)
        if m:
            if cur_round is not None:
                cur_round["scored"] = _parse_comma_int(m.group(2))
                cur_round["needed"] = _parse_comma_int(m.group(3))
                cur_round["won"] = (m.group(4) == "WON")
                cur_round["hands_used"] = int(m.group(5))
                cur_round["discards_used"] = int(m.group(6))
                rounds.append(cur_round)
                cur_round = None
            in_shop = False
            continue

    # ── Build antes list ──────────────────────────────────────────────────────
    # Group rounds by ante, preserving ante_order
    rounds_by_ante: dict[int, list] = defaultdict(list)
    for r in rounds:
        rounds_by_ante[r["ante"]].append(r)

    # Collect all ante nums that appear (from metadata or rounds)
    all_ante_nums_seen: list[int] = []
    seen: set[int] = set()
    for an in ante_order:
        if an not in seen:
            all_ante_nums_seen.append(an)
            seen.add(an)
    for r in rounds:
        if r["ante"] not in seen:
            all_ante_nums_seen.append(r["ante"])
            seen.add(r["ante"])
    all_ante_nums_seen.sort()

    for an in all_ante_nums_seen:
        meta = ante_meta.get(an, _new_ante_meta(an))
        result["antes"].append({
            "ante_num": an,
            "roster": meta["roster"],
            "deck": meta["deck"],
            "strategy": meta["strategy"],
            "money": meta["money"],
            "levels": meta["levels"],
            "rounds": rounds_by_ante.get(an, []),
        })

    return result


def _fmt_shop(shop: dict) -> str:
    """Render shop actions as a compact one-liner."""
    parts: list[str] = []
    for sell in shop["sells"]:
        # Shorten sell descriptions: "sell JokerName (value=...)" → "sold JokerName"
        name = re.sub(r"\s*\(.*", "", sell).strip()
        # Handle patterns like "Diet Cola: sell for free shop reroll"
        if ":" in name:
            name = name.split(":")[0].strip()
        # "sell decayed Popcorn (+4.0 Mult, about to disappear)" → "Popcorn"
        name = re.sub(r"^sell\s+", "", name, flags=re.I).strip()
        name = re.sub(r"\s*\(.*", "", name).strip()
        parts.append(f"sold {name}")
    for buy in shop["buys"]:
        if buy["type"] == "joker":
            parts.append(f"+{buy['name']} ${buy['cost']}")
        elif buy["type"] == "consumable":
            parts.append(f"+{buy['name']} ${buy['cost']}")
        elif buy["type"] == "pack":
            parts.append(f"+[{buy['name']} ${buy['cost']}]")
        elif buy["type"] == "voucher":
            parts.append(f"+voucher:{buy['name']} ${buy['cost']}")
    for pick in shop["packs"]:
        if pick["type"] == "planet":
            # "Mercury (levels Pair, affinity=0)" → "Mercury(Pair)"
            lvl = re.sub(r",.*", "", pick["levels"]).strip()
            parts.append(f"+{pick['name']}({lvl})")
        else:
            parts.append(f"+{pick['name']}")
    return "  ".join(parts) if parts else "—"


def generate_win_replay_md(batch_label: str, replays: list[dict]) -> str:
    """Render a list of parsed winning game dicts to markdown."""
    lines: list[str] = []
    w = lines.append

    w(f"# {batch_label} — Win Replays")
    w("")
    w(f"*{len(replays)} winning game{'s' if len(replays) != 1 else ''}*")

    for game in replays:
        w("")
        w("---")
        w("")

        seed = game["seed"]
        deck = game["deck"].capitalize()
        stake = game["stake"].capitalize()
        fa = game["final_ante"]
        hands = game["total_hands"]
        discs = game["total_discards"]
        money = game["final_money"]
        jokers = game["final_jokers"]

        w(f"## SEED: {seed} | {deck} / {stake} | Won ante {fa} | {hands} hands {discs} discards | ${money}")
        w(f"Final jokers: {jokers}")

        # Identify turning point: first ante where every round has overkill > 3.0
        turning_ante = None
        for ante in game["antes"]:
            an = ante["ante_num"]
            an_rounds = ante["rounds"]
            if an_rounds and all(
                r["won"] and r["needed"] > 0 and r["scored"] / r["needed"] > 3.0
                for r in an_rounds
            ):
                turning_ante = an
                break

        for ante in game["antes"]:
            an = ante["ante_num"]
            roster = ante["roster"]
            strategy = ante["strategy"]
            money_str = f"${ante['money']}" if ante["money"] else "?"
            deck_line = ante["deck"]

            tp_flag = " 🔥" if an == turning_ante else ""
            w("")
            w(f"### Ante {an}{tp_flag} — [{roster}] | {money_str} | {strategy}")

            if deck_line:
                w(f"Deck {deck_line}")

            for rnd in ante["rounds"]:
                w("")
                boss_tag = " *(boss)*" if rnd["is_boss"] else ""
                needed_fmt = f"{rnd['needed']:,}"
                w(f"**{rnd['blind']}{boss_tag}** — {needed_fmt} chips")

                # Initial hand + best assessment
                hand_str = rnd["initial_hand"]
                debuffed = rnd["initial_debuffed"]
                best = rnd["best_hand"]
                best_score = rnd["best_score"]
                can_win = rnd["can_win"]
                win_tag = "CAN WIN" if can_win else "NEED MORE"
                hand_display = f"[{hand_str}]" if hand_str else "?"
                if debuffed:
                    hand_display += f" | DEBUFFED: [{debuffed}]"
                if best:
                    w(f"> Hand: {hand_display} | best: {best} = {best_score:,} — {win_tag}")
                else:
                    w(f"> Hand: {hand_display}")

                # Actions
                running = 0
                plays = [a for a in rnd["actions"] if a["type"] == "play"]
                last_play_idx = None
                for i, a in enumerate(rnd["actions"]):
                    if a["type"] == "play":
                        last_play_idx = i

                for i, action in enumerate(rnd["actions"]):
                    if action["type"] == "discard":
                        cards = action["cards"]
                        if action["chase_hand"]:
                            pct = action["chase_pct"]
                            ratio = action["chase_ratio"]
                            w(f"- Discard [{cards}] → chasing {action['chase_hand']} ({pct}%, {ratio}x EV)")
                        else:
                            # Non-chase discard (hopeless redraw, etc.)
                            reason = action["reason"]
                            # Shorten
                            reason = re.sub(r"\[EV.*\]", "", reason).strip().rstrip(",").strip()
                            w(f"- Discard [{cards}] ({reason})")

                    elif action["type"] == "play":
                        score = action["score"]
                        running += score
                        ht = action["hand_type"]
                        cards = action["cards"]
                        is_last = (i == last_play_idx)
                        if is_last:
                            if rnd["won"]:
                                sc = rnd["scored"]
                                nd = rnd["needed"]
                                ratio = sc / nd if nd else 0
                                close_flag = " ⚠️" if ratio < 1.15 else ""
                                hd = rnd["hands_used"]
                                dd = rnd["discards_used"]
                                w(f"- **Play: {ht} [{cards}] = {score:,}** ✓ WON {sc:,}/{nd:,} ({hd}h {dd}d){close_flag}")
                            else:
                                sc = rnd["scored"]
                                nd = rnd["needed"]
                                hd = rnd["hands_used"]
                                dd = rnd["discards_used"]
                                w(f"- **Play: {ht} [{cards}] = {score:,}** ✗ LOST {sc:,}/{nd:,} ({hd}h {dd}d)")
                        else:
                            nd = rnd["needed"]
                            remaining = max(0, nd - running)
                            w(f"- Play: {ht} [{cards}] = {score:,} ({remaining:,} remaining)")

                    elif action["type"] == "use":
                        w(f"- Use: {action['detail']}")

                # Shop summary
                shop_str = _fmt_shop(rnd["shop"])
                if shop_str and shop_str != "—":
                    w(f"Shop: {shop_str}")

    w("")
    return "\n".join(lines)
