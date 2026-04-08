from __future__ import annotations

from collections import Counter, defaultdict
from itertools import combinations


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
    stake_stats = stats.get("stake_stats", {})
    stake_str = ", ".join(f"{s} ×{c}" for s, c in sorted(stake_stats.items(), key=lambda x: -x[1])) if stake_stats else "—"
    header_rows = [
        ["Games", str(total)],
        ["Wins", f"{wins} ({_pct(wins, total)})"],
        ["Stake", stake_str],
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
            tarot_rows = [[name, str(count)] for name, count in stats["tarot_buys"].most_common(1000)]
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
            jb_rows = [[name, str(count)] for name, count in stats["joker_buys"].most_common(1000)]
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
        diffs = stats["mismatch_diffs"]
        actual_values = stats.get("actual_values", [])
        if diffs:
            avg_diff = sum(diffs) / len(diffs)
            over = sum(1 for d in diffs if d < 0)
            under = len(diffs) - over
            acc_rows.append(["Avg diff", f"{avg_diff:+,.0f} chips"])
            acc_rows.append(["Over-estimates", f"{over} ({_pct(over, len(diffs))})"])
            acc_rows.append(["Under-estimates", f"{under} ({_pct(under, len(diffs))})"])
            if actual_values:
                avg_actual = sum(actual_values) / len(actual_values)
                if avg_actual > 0:
                    bias_pct = 100 * avg_diff / avg_actual
                    acc_rows.append(["Systematic bias", f"{bias_pct:+.1f}% of actual score"])
        table(["Metric", "Value"], acc_rows, right_cols={1})

        # Diff magnitude buckets
        if diffs:
            w("")
            w("### Mismatch Magnitude")
            w("")
            abs_diffs = [abs(d) for d in diffs]
            buckets = [
                ("<50",    sum(1 for d in abs_diffs if d < 50)),
                ("50–499", sum(1 for d in abs_diffs if 50 <= d < 500)),
                ("500–4999", sum(1 for d in abs_diffs if 500 <= d < 5000)),
                ("5000+",  sum(1 for d in abs_diffs if d >= 5000)),
            ]
            bkt_rows = [[label, str(count), _pct(count, len(diffs))]
                        for label, count in buckets if count > 0]
            table(["Diff size", "Count", "%"], bkt_rows, right_cols={1, 2})

        # Mismatch rate by hand type
        scored_by_hand = stats.get("scored_by_hand", Counter())
        mismatch_by_hand = stats.get("mismatch_by_hand", Counter())
        if scored_by_hand:
            w("")
            w("### Mismatch Rate by Hand Type")
            w("")
            ht_rows = []
            for ht, scored in scored_by_hand.most_common():
                mm_count = mismatch_by_hand.get(ht, 0)
                ht_rows.append([ht, str(scored), str(mm_count), _pct(mm_count, scored)])
            table(["Hand Type", "Scored", "Mismatches", "Rate"], ht_rows, right_cols={1, 2, 3})

        # Joker lift — which jokers appear disproportionately in mismatches
        joker_in_all = stats.get("joker_in_all", Counter())
        joker_in_mismatch = stats.get("joker_in_mismatch", Counter())
        if joker_in_mismatch and stats["total_scores"] > 0 and stats["mismatches"] > 0:
            overall_mismatch_rate = stats["mismatches"] / stats["total_scores"]
            lift_rows = []
            for jname, mm_count in joker_in_mismatch.most_common():
                total_appearances = joker_in_all.get(jname, 0)
                if total_appearances < 3:
                    continue
                joker_mismatch_rate = mm_count / total_appearances
                lift = joker_mismatch_rate / overall_mismatch_rate if overall_mismatch_rate > 0 else 0
                lift_rows.append((jname, mm_count, total_appearances, joker_mismatch_rate, lift))
            # Sort by lift descending, show only jokers with lift > 1.5
            lift_rows.sort(key=lambda x: -x[4])
            lift_rows = [r for r in lift_rows if r[4] >= 1.5]
            if lift_rows:
                w("")
                w("### Joker Mismatch Lift")
                w("")
                w("Jokers appearing disproportionately often in mismatches. Lift = joker's mismatch rate / overall rate.")
                w("")
                lr_rows = [[name, str(mm), str(total), _pct(mm, total), f"{lift:.1f}x"]
                           for name, mm, total, _, lift in lift_rows[:15]]
                table(["Joker", "Mismatches", "Appearances", "Rate", "Lift"], lr_rows, right_cols={1, 2, 3, 4})

        def _lift_table(heading: str, in_all: Counter, in_mismatch: Counter,
                        label_col: str, min_appearances: int = 3) -> None:
            if not in_mismatch or stats["total_scores"] == 0 or stats["mismatches"] == 0:
                return
            overall_rate = stats["mismatches"] / stats["total_scores"]
            rows = []
            for val, mm_count in in_mismatch.most_common():
                total_appearances = in_all.get(val, 0)
                if total_appearances < min_appearances:
                    continue
                val_rate = mm_count / total_appearances
                lift = val_rate / overall_rate
                rows.append((val, mm_count, total_appearances, val_rate, lift))
            rows.sort(key=lambda x: -x[4])
            rows = [r for r in rows if r[4] >= 1.5]
            if not rows:
                return
            w("")
            w(f"### {heading}")
            w("")
            tbl = [[val, str(mm), str(tot), _pct(mm, tot), f"{lift:.1f}x"]
                   for val, mm, tot, _, lift in rows[:15]]
            table([label_col, "Mismatches", "Appearances", "Rate", "Lift"], tbl, right_cols={1, 2, 3, 4})

        if stats["total_scores"] > 0:
            _lift_table(
                "Rank Mismatch Lift",
                stats.get("rank_in_all", Counter()), stats.get("rank_in_mismatch", Counter()),
                "Rank",
            )
            _lift_table(
                "Suit Mismatch Lift",
                stats.get("suit_in_all", Counter()), stats.get("suit_in_mismatch", Counter()),
                "Suit",
            )
            _lift_table(
                "Enhancement Mismatch Lift",
                stats.get("enh_in_all", Counter()), stats.get("enh_in_mismatch", Counter()),
                "Enhancement", min_appearances=2,
            )
            _lift_table(
                "Seal Mismatch Lift",
                stats.get("seal_in_all", Counter()), stats.get("seal_in_mismatch", Counter()),
                "Seal", min_appearances=2,
            )
            _lift_table(
                "Edition Mismatch Lift",
                stats.get("ed_in_all", Counter()), stats.get("ed_in_mismatch", Counter()),
                "Edition", min_appearances=2,
            )
            _lift_table(
                "Blind Mismatch Lift",
                stats.get("blind_in_all", Counter()), stats.get("blind_in_mismatch", Counter()),
                "Blind",
            )
            _lift_table(
                "Ante Mismatch Lift",
                stats.get("ante_in_all", Counter()), stats.get("ante_in_mismatch", Counter()),
                "Ante",
            )
            _lift_table(
                "Hands-Left Mismatch Lift",
                stats.get("hands_left_in_all", Counter()), stats.get("hands_left_in_mismatch", Counter()),
                "Hands Left",
            )

        # Combo lift — pairs and triples of attributes that mismatch together
        def _combo_lift_table(heading: str, in_all: Counter, in_mismatch: Counter,
                              min_appearances: int, min_lift: float,
                              prune_parents: Counter | None = None,
                              parent_all: Counter | None = None) -> None:
            if not in_mismatch or stats["total_scores"] == 0 or stats["mismatches"] == 0:
                return
            overall_rate = stats["mismatches"] / stats["total_scores"]
            rows = []
            for combo, mm_count in in_mismatch.most_common():
                total_appearances = in_all.get(combo, 0)
                if total_appearances < min_appearances:
                    continue
                combo_rate = mm_count / total_appearances
                lift = combo_rate / overall_rate
                if lift < min_lift:
                    continue
                # Redundancy pruning: skip triples where a constituent pair
                # already has >= 95% mismatch rate
                if prune_parents and parent_all:
                    parts = combo.split(" + ")
                    redundant = False
                    for pair in combinations(parts, 2):
                        pair_key = " + ".join(pair)
                        pair_total = parent_all.get(pair_key, 0)
                        pair_mm = prune_parents.get(pair_key, 0)
                        if pair_total >= min_appearances and pair_mm / pair_total >= 0.95:
                            redundant = True
                            break
                    if redundant:
                        continue
                rows.append((combo, mm_count, total_appearances, combo_rate, lift))
            rows.sort(key=lambda x: -x[4])
            if not rows:
                return
            w("")
            w(f"### {heading}")
            w("")
            w("Attribute combinations appearing disproportionately in mismatches.")
            w("")
            tbl = [[combo, str(mm), str(tot), _pct(mm, tot), f"{lift:.1f}x"]
                   for combo, mm, tot, _, lift in rows[:20]]
            table(["Combo", "Mismatches", "Appearances", "Rate", "Lift"], tbl, right_cols={1, 2, 3, 4})

        combo2_all = stats.get("combo2_in_all", Counter())
        combo2_mm = stats.get("combo2_in_mismatch", Counter())
        combo3_all = stats.get("combo3_in_all", Counter())
        combo3_mm = stats.get("combo3_in_mismatch", Counter())

        _combo_lift_table(
            "Pair Combo Mismatch Lift", combo2_all, combo2_mm,
            min_appearances=3, min_lift=2.0,
        )
        _combo_lift_table(
            "Triple Combo Mismatch Lift", combo3_all, combo3_mm,
            min_appearances=2, min_lift=3.0,
            prune_parents=combo2_mm, parent_all=combo2_all,
        )

    milk_total = sum(stats["milk_actions"].values())
    if milk_total:
        w("")
        w(f"## Milk Actions ({milk_total} total)")
        w("")
        milk_rows = [[action, str(count)] for action, count in stats["milk_actions"].most_common(10)]
        table(["Action", "Count"], milk_rows, right_cols={1})

    w("")
    return "\n".join(lines)
