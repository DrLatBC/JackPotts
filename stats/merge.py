from __future__ import annotations

from collections import Counter, defaultdict


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
        "stake_stats": Counter(),
        "hand_types_by_ante": defaultdict(Counter),
        "total_scores": 0,
        "mismatches": 0,
        "mismatch_diffs": [],
        "scored_by_hand": Counter(),
        "mismatch_by_hand": Counter(),
        "joker_in_all": Counter(),
        "joker_in_mismatch": Counter(),
        "rank_in_all": Counter(),
        "rank_in_mismatch": Counter(),
        "suit_in_all": Counter(),
        "suit_in_mismatch": Counter(),
        "enh_in_all": Counter(),
        "enh_in_mismatch": Counter(),
        "seal_in_all": Counter(),
        "seal_in_mismatch": Counter(),
        "ed_in_all": Counter(),
        "ed_in_mismatch": Counter(),
        "actual_values": [],
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
        for stake, count in s.get("stake_stats", {}).items():
            merged["stake_stats"][stake] += count
        for ante, counter in s.get("hand_types_by_ante", {}).items():
            merged["hand_types_by_ante"][ante] += counter
        merged["total_scores"] += s.get("total_scores", 0)
        merged["mismatches"] += s.get("mismatches", 0)
        merged["mismatch_diffs"].extend(s.get("mismatch_diffs", []))
        merged["scored_by_hand"] += s.get("scored_by_hand", Counter())
        merged["mismatch_by_hand"] += s.get("mismatch_by_hand", Counter())
        merged["joker_in_all"] += s.get("joker_in_all", Counter())
        merged["joker_in_mismatch"] += s.get("joker_in_mismatch", Counter())
        merged["rank_in_all"] += s.get("rank_in_all", Counter())
        merged["rank_in_mismatch"] += s.get("rank_in_mismatch", Counter())
        merged["suit_in_all"] += s.get("suit_in_all", Counter())
        merged["suit_in_mismatch"] += s.get("suit_in_mismatch", Counter())
        merged["enh_in_all"] += s.get("enh_in_all", Counter())
        merged["enh_in_mismatch"] += s.get("enh_in_mismatch", Counter())
        merged["seal_in_all"] += s.get("seal_in_all", Counter())
        merged["seal_in_mismatch"] += s.get("seal_in_mismatch", Counter())
        merged["ed_in_all"] += s.get("ed_in_all", Counter())
        merged["ed_in_mismatch"] += s.get("ed_in_mismatch", Counter())
        merged["actual_values"].extend(s.get("actual_values", []))
    return merged
