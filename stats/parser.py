from __future__ import annotations

import re
from collections import Counter, defaultdict
from pathlib import Path

from .constants import (
    KNOWN_HAND_TYPES, PLANET_NAMES, UTILITY_JOKERS, XMULT_JOKERS,
    RE_BEST_AVAILABLE, RE_BEST_AVAILABLE_PLAY, RE_BLIND, RE_CONSUMABLE_BUY,
    RE_DESPERATION_CYCLE, RE_GAME_OVER, RE_GAME_START, RE_JOKER_BUY,
    RE_JOKER_SELL, RE_JOKER_SELL_FOR, RE_MILK_ACTION, RE_MILK_PLAY,
    RE_MOUTH_LOCKED, RE_PACK_PICK, RE_PACK_SKIP, RE_PLAY, RE_REROLL,
    RE_ROSTER, RE_ROSTER_SCALING, RE_ROUND, RE_SHOP_MONEY, RE_SUMMARY,
    RE_TAROT_DESPERATE, RE_TAROT_USE, RE_VOUCHER_BUY,
)


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
    cur_stake = "UNKNOWN"       # stake for current game
    stake_stats: Counter = Counter()
    cur_roster_line = ""        # last seen Roster line, parsed on game over
    jokers_bought_this_game: set[str] = set()
    deck_stats: dict[str, list] = defaultdict(list)
    hand_types_by_ante: dict[int, Counter] = defaultdict(Counter)

    for line in text.splitlines():
        # Game start — capture deck type
        gs = RE_GAME_START.search(line)
        if gs:
            cur_deck = gs.group(1)
            cur_stake = gs.group(2)
            stake_stats[cur_stake] += 1

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
        "stake_stats": dict(stake_stats),
        "hand_types_by_ante": dict(hand_types_by_ante),
    }
