"""Test boss-blind scoring mismatches for scaling/state-dependent jokers.

Reproduces bugs from DrLatBC/balatro-bot#14 and related mismatch clusters.

Two test modes per bucket:
  1. NAIVE baseline (SB+BB): fresh gamestate -> play first 5 -> compare.
     Proves the scoring formula is correct when given accurate inputs.
  2. ENGINE mode (boss blind): uses the real rule engine to decide actions.
     State flows from action responses (no fresh gamestate between actions),
     replicating the exact production bot loop.  If mismatches appear here
     but not in naive mode, the state-flow is the culprit.

Usage:
    python test_hook_scaling.py [--port PORT] [--seed SEED] [--bucket A|B|C|D|E|all]

Requires a running balatrobot server.
"""

import argparse
import logging
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from balatrobot.cli.client import BalatroClient, APIError
from balatro_bot.domain.scoring.classify import classify_hand, _scoring_cards_for
from balatro_bot.engine import RuleEngine
from balatro_bot.cards import is_joker_debuffed

from harness import (
    TEST_PORT, BOSS_MIN_ANTE,
    wait_for_state, setup_game, get_current_blind, get_boss_name,
    is_boss_blind_select, beat_blind_fast, cheat_win_if_needed,
    advance_through_post_blind, force_boss, inject_jokers, set_ante,
    inject_god_mode, inject_milk_trigger, burn_discards, snapshot_jokers,
    ensure_server, stop_server, take_screenshot,
)
from scoring_diagnostics import (
    fmt_card, fmt_joker_ability, dump_hand_detail, dump_joker_diff,
    build_snapshot, score_snapshot, play_hand_and_score,
)
from bucket_defs import BUCKET_DEFS


# ---------------------------------------------------------------------------
# Naive play (baseline) -- fresh gamestate before each hand
# ---------------------------------------------------------------------------

def play_naive_hands(client, num_hands, blind_name, track_keys):
    """Play hands naively (fresh gamestate, first 5 cards). Baseline mode."""
    results = []
    for h in range(num_hands):
        state = client.call("gamestate")
        gs = state.get("state", "")
        if gs != "SELECTING_HAND":
            break
        cur_blind, _ = get_current_blind(state)
        if cur_blind != blind_name:
            break

        hand_cards = state.get("hand", {}).get("cards", [])
        n = min(5, len(hand_cards))
        indices = list(range(n))

        pre_snap = snapshot_jokers(state, track_keys)
        result = play_hand_and_score(client, state, indices, blind_name)
        if result is None:
            break

        result["pre_snap"] = pre_snap
        tag = "MATCH" if result["diff"] == 0 else f"MISMATCH({result['diff']:+d})"
        snap_str = "  ".join(f"{k}={v}" for k, v in pre_snap.items())
        print(f"\n    h{h+1} [naive]: {result['hand_name']:20s} est={result['est']:>8d} actual={result['actual']:>8d} {tag:>16s}  | {snap_str}")

        # Always dump full detail
        dump_hand_detail(
            result["detail"], result["played"], result["held"],
            result["scoring"], result["jokers"], result["state"],
            result["pre_chips"], result["actual"], prefix="      ")
        post_jokers = result["new_state"].get("jokers", {}).get("cards", [])
        dump_joker_diff(result["jokers"], post_jokers, prefix="      ")

        if result["diff"] != 0:
            take_screenshot(client, f"mismatch_naive_h{h+1}_{blind_name}")

        results.append(result)

        time.sleep(0.3)
        for _ in range(20):
            state = client.call("gamestate")
            gs = state.get("state", "")
            if gs in ("SELECTING_HAND", "ROUND_EVAL", "SHOP", "BLIND_SELECT", "GAME_OVER"):
                break
            time.sleep(0.3)
    return results


# ---------------------------------------------------------------------------
# Engine play (production-like) -- rule engine decides, state flows from
# action responses, snapshot built the same way bot.py does it.
# ---------------------------------------------------------------------------

def play_engine_hands(client, blind_name, track_keys, max_actions=50):
    """Play a blind using the real rule engine, replicating the bot loop.

    State flows from action responses -- NO fresh gamestate calls between actions.
    Returns list of scoring result dicts (only for play actions).
    """
    engine = RuleEngine()
    results = []

    state = client.call("gamestate")
    actions_taken = 0

    for _ in range(max_actions):
        gs = state.get("state", "")
        if gs != "SELECTING_HAND":
            break

        cur_blind, _ = get_current_blind(state)
        if cur_blind != blind_name:
            break

        action = engine.decide(state)
        if action is None:
            time.sleep(0.15)
            state = client.call("gamestate")
            continue

        method, params = action.to_rpc()
        actions_taken += 1

        state_source = "action_response"

        pre_play_chips = 0
        snapshot = None
        if method == "play":
            pre_play_chips = state.get("round", {}).get("chips", 0)
            hand_name = getattr(action, "hand_name", "")
            snapshot = build_snapshot(state, params, hand_name)

        fresh_snap = None
        if method == "play":
            fresh_state = client.call("gamestate")
            fresh_snap = snapshot_jokers(fresh_state, track_keys)

        try:
            state = client.call(method, params)
        except APIError as e:
            print(f"    Action {method} failed: {e.message}")
            state = client.call("gamestate")
            continue

        if method == "play" and snapshot:
            detail, scoring = score_snapshot(snapshot)
            post_chips = state.get("round", {}).get("chips", 0)
            actual = post_chips - pre_play_chips
            diff = actual - detail["total"]

            pre_snap = {}
            for jk in snapshot["jokers"]:
                key = jk.get("key", "")
                if key in track_keys:
                    pre_snap[key] = dict(jk.get("ability", {}))

            tag = "MATCH" if diff == 0 else f"MISMATCH({diff:+d})"
            reason = getattr(action, "reason", "")[:80]
            snap_str = "  ".join(f"{k}={v}" for k, v in pre_snap.items())

            stale_vs_fresh = ""
            if fresh_snap and pre_snap:
                diffs_found = []
                for k in track_keys:
                    stale_ab = pre_snap.get(k, {})
                    fresh_ab = fresh_snap.get(k, {})
                    if stale_ab != fresh_ab:
                        diffs_found.append(f"{k}: stale={stale_ab} fresh={fresh_ab}")
                if diffs_found:
                    stale_vs_fresh = f"\n           STALE vs FRESH: {'; '.join(diffs_found)}"

            print(f"\n    h{len(results)+1} [engine]: {detail['hand_name']:20s} est={detail['total']:>8d} actual={actual:>8d} {tag:>16s}  | {snap_str}")
            if reason:
                print(f"           reason: {reason}")
            if stale_vs_fresh:
                print(stale_vs_fresh)

            dump_state = {
                "money": snapshot["money"],
                "round": {
                    "discards_left": snapshot["discards_left"],
                    "hands_left": snapshot["hands_left"],
                    "chips": pre_play_chips,
                },
                "cards": {"count": snapshot.get("deck_count", 0)},
                "ante_num": snapshot.get("ante", "?"),
            }
            dump_hand_detail(
                detail, snapshot["played"], snapshot["held"],
                scoring, snapshot["jokers"], dump_state,
                pre_play_chips, actual, prefix="      ")
            post_jokers = state.get("jokers", {}).get("cards", [])
            dump_joker_diff(snapshot["jokers"], post_jokers, prefix="      ")

            if diff != 0:
                take_screenshot(client, f"mismatch_engine_h{len(results)+1}_{blind_name}")

            results.append({
                "hand_name": detail["hand_name"],
                "est": detail["total"],
                "actual": actual,
                "diff": diff,
                "detail": detail,
                "joker_snapshot": pre_snap,
                "fresh_snapshot": fresh_snap,
                "pre_snap": pre_snap,
                "state_source": state_source,
                "reason": getattr(action, "reason", ""),
            })
        elif method != "play":
            reason = getattr(action, "reason", "")[:80]
            print(f"    [{actions_taken}] {method:12s} | {reason}")

        time.sleep(0.15)

    return results


# ---------------------------------------------------------------------------
# Bucket runner
# ---------------------------------------------------------------------------

def run_bucket(client, bucket_id, seed, engine_only=False):
    bdef = BUCKET_DEFS[bucket_id]
    boss_name = bdef["boss"]
    joker_keys = bdef["jokers"]
    track_keys = set(bdef["track_keys"])
    ante = bdef.get("ante") or BOSS_MIN_ANTE.get(boss_name, 1)

    print(f"\n{'='*70}")
    print(f"BUCKET {bucket_id}: {bdef['label']}")
    print(f"{'='*70}")

    sb_results = []
    bb_results = []
    boss_naive = []
    entry_snap = {}

    if not engine_only:
        state = setup_game(client, f"{seed}_{bucket_id}")
        set_ante(client, ante)
        try:
            client.call("set", {"money": 999999})
        except APIError:
            pass
        inject_jokers(client, joker_keys)
        if not bdef.get("no_god_mode"):
            inject_god_mode(client)
            inject_milk_trigger(client)

        # --- Small Blind (naive baseline) ---
        print(f"\n  --- Small Blind (naive baseline) ---")
        state = client.call("gamestate")
        blind_name, _ = get_current_blind(state)
        print(f"    Blind: {blind_name}")
        if bdef.get("burn_discards"):
            burn_discards(client, target_discards=20)
        sb_results = play_naive_hands(client, 2, blind_name, track_keys)
        cheat_win_if_needed(client, blind_name)
        state = advance_through_post_blind(client)

        if state.get("state") == "GAME_OVER":
            print("    Game over after SB")
            return None

        # --- Big Blind (naive baseline) ---
        print(f"\n  --- Big Blind (naive baseline) ---")
        gs = state.get("state", "")
        if gs == "BLIND_SELECT":
            client.call("select")
            time.sleep(0.3)
        state = wait_for_state(client, {"SELECTING_HAND"}, screenshot_label=f"bucket_{bucket_id}")
        blind_name, _ = get_current_blind(state)
        print(f"    Blind: {blind_name}")
        bb_results = play_naive_hands(client, 2, blind_name, track_keys)
        cheat_win_if_needed(client, blind_name)
        state = advance_through_post_blind(client)

        if state.get("state") == "GAME_OVER":
            print("    Game over after BB")
            return None

        # --- Force boss blind ---
        print(f"\n  --- Forcing {boss_name} ---")
        gs = state.get("state", "")
        if gs != "BLIND_SELECT":
            state = wait_for_state(client, {"BLIND_SELECT"}, screenshot_label=f"bucket_{bucket_id}_force_boss")

        if not force_boss(client, boss_name):
            print(f"    FAILED to force {boss_name}")
            return {"sb": sb_results, "bb": bb_results, "boss_naive": [], "boss_engine": [],
                    "forced": False, "boss_name": boss_name}

        # --- Boss blind: NAIVE play (fresh gamestate each hand) ---
        print(f"\n  --- {boss_name} -- NAIVE mode (fresh gamestate) ---")
        client.call("select")
        time.sleep(0.3)
        state = wait_for_state(client, {"SELECTING_HAND"}, screenshot_label=f"bucket_{bucket_id}")
        blind_name, _ = get_current_blind(state)
        print(f"    Blind: {blind_name}")

        entry_snap = snapshot_jokers(state, track_keys)
        print(f"    Entry snapshot: {entry_snap}")

        boss_naive = play_naive_hands(client, 3, blind_name, track_keys)
        cheat_win_if_needed(client, blind_name)
        state = advance_through_post_blind(client)

        if state.get("state") == "GAME_OVER":
            print("    Game over after naive boss -- skipping engine test")
            return {"sb": sb_results, "bb": bb_results, "boss_naive": boss_naive,
                    "boss_engine": [], "forced": True, "boss_name": boss_name,
                    "entry_snap": entry_snap}

    # --- Start fresh for ENGINE test on same boss ---
    print(f"\n  --- Re-setup for ENGINE test ---")
    state = setup_game(client, f"{seed}_{bucket_id}_ENG")
    set_ante(client, ante)
    try:
        client.call("set", {"money": 999999})
    except APIError:
        pass
    inject_jokers(client, joker_keys)
    if not bdef.get("no_god_mode"):
        inject_god_mode(client)
        inject_milk_trigger(client)

    # Fast-forward through SB+BB
    print(f"    Fast-forwarding SB+BB...")
    burned_in_engine = False
    for _ in range(50):
        state = client.call("gamestate")
        gs = state.get("state", "")
        if gs == "SELECTING_HAND":
            cur_blind, _ = get_current_blind(state)
            if cur_blind not in ("Small Blind", "Big Blind"):
                break
            if bdef.get("burn_discards") and not burned_in_engine:
                burn_discards(client, target_discards=20)
                burned_in_engine = True
            beat_blind_fast(client, state)
        elif gs == "ROUND_EVAL":
            try:
                client.call("cash_out")
            except APIError:
                pass
            time.sleep(0.3)
        elif gs == "SHOP":
            client.call("next_round")
            time.sleep(0.3)
        elif gs == "BLIND_SELECT":
            if is_boss_blind_select(state):
                break
            client.call("select")
            time.sleep(0.3)
        else:
            time.sleep(0.3)

    # Force boss again
    if not force_boss(client, boss_name):
        print(f"    FAILED to force {boss_name} for engine test")
        return {"sb": sb_results, "bb": bb_results, "boss_naive": boss_naive,
                "boss_engine": [], "forced": True, "boss_name": boss_name,
                "entry_snap": entry_snap}

    # --- Boss blind: ENGINE play (production bot loop) ---
    print(f"\n  --- {boss_name} -- ENGINE mode (rule engine, state from responses) ---")
    client.call("select")
    time.sleep(0.3)
    state = wait_for_state(client, {"SELECTING_HAND"})
    blind_name, _ = get_current_blind(state)
    print(f"    Blind: {blind_name}")

    engine_entry_snap = snapshot_jokers(state, track_keys)
    print(f"    Entry snapshot: {engine_entry_snap}")

    boss_engine = play_engine_hands(client, blind_name, track_keys)

    # Cheat to finish if still going
    for _ in range(5):
        state = client.call("gamestate")
        gs = state.get("state", "")
        cur_blind, _ = get_current_blind(state)
        if gs == "SELECTING_HAND" and cur_blind == blind_name:
            beat_blind_fast(client, state)
            time.sleep(0.5)
        else:
            break

    return {
        "sb": sb_results,
        "bb": bb_results,
        "boss_naive": boss_naive,
        "boss_engine": boss_engine,
        "forced": True,
        "boss_name": boss_name,
        "entry_snap": entry_snap,
        "engine_entry_snap": engine_entry_snap,
    }


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def analyze_bucket(bucket_id, data):
    if data is None:
        print(f"\n  Bucket {bucket_id}: ABORTED (game over)")
        return 0, 0, 0, [], []

    bdef = BUCKET_DEFS[bucket_id]
    print(f"\n{'='*70}")
    print(f"RESULTS -- Bucket {bucket_id}: {bdef['label']}")
    print(f"{'='*70}")

    baseline_mm = 0
    baseline_total = 0
    for phase in ("sb", "bb"):
        for r in data.get(phase, []):
            baseline_total += 1
            if r["diff"] != 0:
                baseline_mm += 1

    print(f"  Baseline (SB+BB): {baseline_total} hands, {baseline_mm} mismatches")
    if baseline_mm > 0:
        for phase, name in [("sb", "Small Blind"), ("bb", "Big Blind")]:
            for r in data.get(phase, []):
                if r["diff"] != 0:
                    print(f"    {name}: est={r['est']} actual={r['actual']} diff={r['diff']:+d}")

    boss_name = data.get("boss_name", "?")

    naive_results = data.get("boss_naive", [])
    naive_mm = sum(1 for r in naive_results if r["diff"] != 0)
    naive_diffs = [r["diff"] for r in naive_results]
    print(f"  {boss_name} NAIVE:  {len(naive_results)} hands, {naive_mm} mismatches  diffs={naive_diffs}")

    engine_results = data.get("boss_engine", [])
    engine_mm = sum(1 for r in engine_results if r["diff"] != 0)
    engine_diffs = [r["diff"] for r in engine_results]
    print(f"  {boss_name} ENGINE: {len(engine_results)} hands, {engine_mm} mismatches  diffs={engine_diffs}")

    stale_diffs_found = 0
    for r in engine_results:
        fresh = r.get("fresh_snapshot", {})
        stale = r.get("joker_snapshot", {})
        if fresh and stale and fresh != stale:
            stale_diffs_found += 1
    if stale_diffs_found:
        print(f"  !! {stale_diffs_found} engine hands had STALE joker values (response != fresh gamestate)")

    if not data.get("forced"):
        print(f"    SKIPPED -- could not force {boss_name}")

    return baseline_mm, naive_mm, engine_mm, naive_diffs, engine_diffs


def main():
    parser = argparse.ArgumentParser(
        description="Test boss-blind scoring mismatches for scaling jokers (issue #14)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Buckets:\n" + "\n".join(
            f"  {k}: {v['label']}" for k, v in BUCKET_DEFS.items()
        ),
    )
    parser.add_argument("--port", type=int, default=None,
                        help=f"Server port (default: {TEST_PORT} with --start-server, 12346 without)")
    parser.add_argument("--seed", type=str, default="HOOK14")
    parser.add_argument("--bucket", type=str, default="all",
                        help="Which bucket(s) to run: A, B, C, D, E, or 'all' (default: all)")
    parser.add_argument("--engine-only", action="store_true",
                        help="Skip naive baseline, only run engine mode")
    parser.add_argument("--start-server", action="store_true",
                        help="Auto-start a balatrobot server (killed on exit)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)

    port = args.port or (TEST_PORT if args.start_server else 12346)
    server_proc = None

    if args.start_server:
        client, server_proc = ensure_server(port)
    else:
        client = BalatroClient(port=port)

    try:
        client.call("health")
    except Exception as e:
        print(f"No server on port {port}: {e}")
        sys.exit(1)

    if args.bucket.lower() == "all":
        bucket_ids = list(BUCKET_DEFS.keys())
    else:
        bucket_ids = [b.strip().upper() for b in args.bucket.split(",")]
        for b in bucket_ids:
            if b not in BUCKET_DEFS:
                print(f"Unknown bucket '{b}'. Available: {', '.join(BUCKET_DEFS.keys())}")
                sys.exit(1)

    all_data = {}
    for bid in bucket_ids:
        all_data[bid] = run_bucket(client, bid, args.seed, engine_only=args.engine_only)

    # --- Analysis ---
    print(f"\n\n{'#'*70}")
    print(f"# ANALYSIS")
    print(f"{'#'*70}")

    summary = {}
    for bid in bucket_ids:
        summary[bid] = analyze_bucket(bid, all_data[bid])

    # --- Verdict ---
    print(f"\n{'#'*70}")
    print(f"# VERDICT")
    print(f"{'#'*70}")

    for bid in bucket_ids:
        bdef = BUCKET_DEFS[bid]
        data = all_data[bid]
        bl_mm, naive_mm, engine_mm, naive_diffs, engine_diffs = summary[bid]

        if data is None:
            verdict = "ABORTED"
        elif not data.get("forced"):
            verdict = "SKIP (force failed)"
        elif engine_mm > 0 and naive_mm == 0:
            verdict = f"STATE-FLOW BUG -- engine {engine_mm} mismatches, naive 0"
        elif engine_mm > 0 and naive_mm > 0:
            verdict = f"BOTH MISMATCH -- engine {engine_mm}, naive {naive_mm}"
        elif engine_mm == 0 and naive_mm == 0:
            verdict = "ALL MATCH (both modes)"
        elif naive_mm > 0 and engine_mm == 0:
            verdict = f"NAIVE ONLY -- {naive_mm} mismatches (unexpected)"
        else:
            verdict = f"engine={engine_mm} naive={naive_mm}"

        print(f"  Bucket {bid} ({bdef['boss']:15s} + {', '.join(bdef['jokers'])})")
        print(f"    {verdict}")

    print(f"\nKey: STATE-FLOW BUG = mismatch only when using rule engine (stale state from action responses)")
    print(f"     BOTH MISMATCH  = mismatch in both modes (API or formula bug)")
    print(f"     ALL MATCH      = no issue detected with this seed")
    print(f"\nRe-run with different --seed values to confirm consistency.")

    if server_proc:
        stop_server(server_proc)


if __name__ == "__main__":
    main()
