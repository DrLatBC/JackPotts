"""Integration test: Flint + Luchador sell → _boss_disabled lifecycle.

Reproduces the batch 075 mismatches where selling Luchador during The Flint
boss blind should disable the halving effect, but the bot continues to apply
(or not apply) Flint halving incorrectly due to _boss_disabled being lost
when state = client.call(method, params) replaces the state dict.

Test flow:
  1. Set up game at The Flint boss blind with Green Joker + Luchador
  2. Play hand #1 with Flint active — verify halving is applied and scoring matches
  3. Sell Luchador to disable The Flint
  4. Dump raw blind API data before/after sell to see what changes
  5. Play hand #2 with Flint disabled — verify halving is NOT applied
  6. Compare estimate vs actual for both hands

Usage:
    python test_flint_luchador.py [--port PORT] [--seed SEED] [--start-server]
"""

import argparse
import json
import logging
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from balatrobot.cli.client import BalatroClient, APIError
from balatro_bot.domain.scoring.classify import classify_hand, _scoring_cards_for
from balatro_bot.domain.scoring.estimate import score_hand_detailed
from balatro_bot.domain.scoring.base import flint_halve_hand_levels
from balatro_bot.cards import is_joker_debuffed

from harness import (
    TEST_PORT, BOSS_MIN_ANTE, BOSS_KEYS,
    ensure_server, stop_server,
    wait_for_state, setup_game, get_current_blind, get_boss_name,
    advance_to_boss_select, force_boss, inject_jokers, set_ante,
    beat_blind_fast, advance_through_post_blind,
    take_screenshot, snapshot_jokers,
)
from scoring_diagnostics import (
    fmt_card, fmt_joker_ability, dump_hand_detail, dump_joker_diff,
    build_snapshot, score_snapshot, play_hand_and_score,
)


# ---------------------------------------------------------------------------
# Blind data dump — the core diagnostic we need
# ---------------------------------------------------------------------------

def dump_blind_data(state: dict, label: str) -> dict:
    """Dump raw blind API data from gamestate and return it."""
    blinds = state.get("blinds", {})
    print(f"\n  === BLIND DATA ({label}) ===")
    for bkey, bval in blinds.items():
        if not isinstance(bval, dict):
            print(f"    {bkey}: {bval}")
            continue
        name = bval.get("name", "?")
        status = bval.get("status", "?")
        score = bval.get("score", "?")
        key = bval.get("key", "?")
        # Dump ALL fields to see what changes after Luchador sell
        print(f"    {bkey}: name={name}  key={key}  status={status}  score={score}")
        extra = {k: v for k, v in bval.items() if k not in ("name", "status", "score", "key")}
        if extra:
            print(f"           extra: {extra}")
    return blinds


def dump_hand_levels(state: dict, label: str) -> dict:
    """Dump hand level data from API."""
    hands = state.get("hands", {})
    print(f"\n  === HAND LEVELS ({label}) ===")
    # Only show a few common ones to keep output manageable
    for hname in ("High Card", "Pair", "Three of a Kind", "Straight", "Flush"):
        hl = hands.get(hname, {})
        if hl:
            print(f"    {hname:20s}: chips={hl.get('chips','?')}  mult={hl.get('mult','?')}  level={hl.get('level','?')}")
    return hands


# ---------------------------------------------------------------------------
# Scoring helper — with explicit boss_disabled control
# ---------------------------------------------------------------------------

def score_with_boss_disabled(state: dict, play_indices: list[int],
                              boss_disabled: bool) -> dict:
    """Score a hand from state, explicitly controlling whether Flint halving applies.

    This lets us compare what the bot WOULD estimate with vs without the fix.
    """
    hand_cards = state.get("hand", {}).get("cards", [])
    jokers = state.get("jokers", {}).get("cards", [])
    hand_levels = state.get("hands", {})

    played = [hand_cards[i] for i in play_indices if i < len(hand_cards)]
    held = [c for j, c in enumerate(hand_cards) if j not in set(play_indices)]

    hand_name = classify_hand(played)

    # Apply Flint halving ONLY if boss is NOT disabled
    blind_name = ""
    for b in state.get("blinds", {}).values():
        if isinstance(b, dict) and b.get("status") == "CURRENT":
            blind_name = b.get("name", "")
            break

    if blind_name == "The Flint" and not boss_disabled:
        hand_levels = flint_halve_hand_levels(hand_levels)

    joker_key_set = {j.get("key") for j in jokers if not is_joker_debuffed(j)}
    four_fingers = "j_four_fingers" in joker_key_set
    smeared = "j_smeared" in joker_key_set
    scoring = played if "j_splash" in joker_key_set else _scoring_cards_for(
        hand_name, played, four_fingers=four_fingers, smeared=smeared)

    # Pass blind_name="" when boss disabled (matches _log_played_hand behavior)
    scoring_blind = "" if boss_disabled else blind_name

    detail = score_hand_detailed(
        hand_name, scoring,
        hand_levels=hand_levels,
        jokers=jokers,
        played_cards=played,
        held_cards=held,
        money=state.get("money", 0),
        discards_left=state.get("round", {}).get("discards_left", 0),
        hands_left=state.get("round", {}).get("hands_left", 1),
        joker_limit=state.get("jokers", {}).get("limit", 5),
        blind_name=scoring_blind,
        deck_count=state.get("cards", {}).get("count", 0),
        deck_cards=state.get("cards", {}).get("cards", []),
    )
    return {
        "detail": detail,
        "hand_name": hand_name,
        "played": played,
        "held": held,
        "scoring": scoring,
        "blind_name": blind_name,
        "boss_disabled": boss_disabled,
    }


# ---------------------------------------------------------------------------
# Main test
# ---------------------------------------------------------------------------

def run_flint_luchador_test(client, seed="FLINTLUCH1"):
    """Test The Flint + Luchador sell lifecycle."""

    print("\n" + "=" * 70)
    print("TEST: The Flint + Luchador sell → _boss_disabled lifecycle")
    print("=" * 70)

    # --- Setup: start game, advance to Flint ---
    print("\n  Phase 1: Setup")
    state = setup_game(client, seed)
    set_ante(client, 2)  # Flint min ante = 2
    try:
        client.call("set", {"money": 999999})
    except APIError:
        pass

    # Advance to boss blind select
    state = advance_to_boss_select(client, target_ante=2)
    gs = state.get("state", "")

    # Force The Flint
    if gs == "BLIND_SELECT":
        ok = force_boss(client, "The Flint")
        if not ok:
            print("  ABORT: Could not force The Flint")
            return None
        client.call("select")
        time.sleep(0.5)
    elif gs == "SELECTING_HAND":
        # Already past blind select — check if it's the boss
        blind_name, _ = get_current_blind(state)
        if blind_name not in ("Small Blind", "Big Blind"):
            print(f"  Already at boss: {blind_name}")
        else:
            print(f"  ABORT: At {blind_name}, not boss")
            return None

    state = wait_for_state(client, {"SELECTING_HAND"}, screenshot_label="flint_luch_setup")
    blind_name, blind_key = get_current_blind(state)
    print(f"  At blind: {blind_name} (key={blind_key})")

    if blind_name != "The Flint":
        print(f"  ABORT: Expected The Flint, got {blind_name}")
        return None

    # Clear jokers and inject our test set
    for _ in range(state.get("jokers", {}).get("count", 0)):
        try:
            client.call("sell", {"joker": 0})
        except APIError:
            pass

    # Inject: Green Joker + Luchador (matches batch 075 scenario)
    joker_keys = ["j_green_joker", "j_luchador"]
    inject_jokers(client, joker_keys)

    # Give enough hands/discards to work with
    try:
        client.call("set", {"hands": 6})
        client.call("set", {"discards": 3})
    except APIError:
        pass

    # Refresh state
    state = client.call("gamestate")

    # --- Dump initial blind data ---
    print("\n  Phase 2: Initial state dump")
    blinds_before = dump_blind_data(state, "BEFORE any action")
    hand_levels_before = dump_hand_levels(state, "BEFORE (raw from API)")

    # Show Flint-halved hand levels
    halved = flint_halve_hand_levels(state.get("hands", {}))
    print("\n  === HAND LEVELS (Flint-halved) ===")
    for hname in ("High Card", "Pair", "Three of a Kind", "Straight", "Flush"):
        hl = halved.get(hname, {})
        if hl:
            print(f"    {hname:20s}: chips={hl.get('chips','?')}  mult={hl.get('mult','?')}")

    # --- Phase 3: Play hand #1 with Flint ACTIVE ---
    print("\n  Phase 3: Play hand with Flint ACTIVE (Luchador still present)")
    state = client.call("gamestate")
    hand_cards = state.get("hand", {}).get("cards", [])
    n = min(5, len(hand_cards))
    play_indices = list(range(n))

    # Score with Flint halving ON
    result_active = score_with_boss_disabled(state, play_indices, boss_disabled=False)
    detail_active = result_active["detail"]

    pre_chips = state.get("round", {}).get("chips", 0)
    print(f"  Hand: {result_active['hand_name']}")
    print(f"  Cards: {', '.join(fmt_card(c) for c in result_active['played'])}")
    print(f"  Base (halved): {detail_active['base_chips']}/{detail_active['base_mult']}")
    print(f"  Estimate (with Flint halving): {detail_active['total']}")

    # Also compute what estimate would be WITHOUT halving (bug case)
    result_no_halve = score_with_boss_disabled(state, play_indices, boss_disabled=True)
    print(f"  Estimate (WITHOUT halving): {result_no_halve['detail']['total']}")

    # Play the hand
    jokers_pre = state.get("jokers", {}).get("cards", [])
    try:
        new_state = client.call("play", {"cards": play_indices})
    except APIError as e:
        print(f"  Play failed: {e.message}")
        return None

    post_chips = new_state.get("round", {}).get("chips", 0)
    actual_h1 = post_chips - pre_chips
    diff_h1 = actual_h1 - detail_active["total"]

    tag = "MATCH" if diff_h1 == 0 else f"MISMATCH({diff_h1:+d})"
    print(f"  Actual: {actual_h1}  Diff: {diff_h1:+d}  {tag}")

    # Show joker changes
    jokers_post = new_state.get("jokers", {}).get("cards", [])
    dump_joker_diff(jokers_pre, jokers_post, prefix="    ")

    state = new_state

    # Wait for SELECTING_HAND again
    for _ in range(20):
        gs = state.get("state", "")
        if gs == "SELECTING_HAND":
            break
        time.sleep(0.3)
        state = client.call("gamestate")

    # --- Phase 4: Sell Luchador ---
    print("\n  Phase 4: Sell Luchador")
    state = client.call("gamestate")
    dump_blind_data(state, "BEFORE Luchador sell")

    # Find Luchador index
    jokers = state.get("jokers", {}).get("cards", [])
    luchador_idx = None
    for i, j in enumerate(jokers):
        if j.get("key") == "j_luchador":
            luchador_idx = i
            break

    if luchador_idx is None:
        print("  ERROR: Luchador not found in joker list!")
        return None

    print(f"  Selling Luchador at index {luchador_idx}...")
    try:
        state = client.call("sell", {"joker": luchador_idx})
    except APIError as e:
        print(f"  Sell failed: {e.message}")
        return None

    # Dump state AFTER sell — this is the key diagnostic
    dump_blind_data(state, "AFTER Luchador sell (from sell response)")

    # Also get fresh gamestate to compare
    fresh_state = client.call("gamestate")
    dump_blind_data(fresh_state, "AFTER Luchador sell (fresh gamestate)")

    # Compare blind data from sell response vs fresh gamestate
    print("\n  === SELL RESPONSE vs FRESH GAMESTATE comparison ===")
    sell_blinds = state.get("blinds", {})
    fresh_blinds = fresh_state.get("blinds", {})
    if sell_blinds == fresh_blinds:
        print("    Blind data IDENTICAL in sell response and fresh gamestate")
    else:
        print("    Blind data DIFFERS:")
        for bkey in set(list(sell_blinds.keys()) + list(fresh_blinds.keys())):
            sv = sell_blinds.get(bkey)
            fv = fresh_blinds.get(bkey)
            if sv != fv:
                print(f"      {bkey}: sell={sv}")
                print(f"      {bkey}: fresh={fv}")

    # Check if API reports any 'disabled' field
    print("\n  === Checking for 'disabled' field in blind data ===")
    for bkey, bval in fresh_blinds.items():
        if isinstance(bval, dict):
            disabled = bval.get("disabled")
            if disabled is not None:
                print(f"    {bkey}: disabled={disabled}")
            else:
                print(f"    {bkey}: no 'disabled' field")

    # Dump hand levels after sell
    dump_hand_levels(fresh_state, "AFTER Luchador sell")

    # --- Phase 5: Play hand #2 with Flint DISABLED ---
    print("\n  Phase 5: Play hand with Flint DISABLED (Luchador sold)")
    state = client.call("gamestate")  # fresh state
    hand_cards = state.get("hand", {}).get("cards", [])
    n = min(5, len(hand_cards))
    play_indices = list(range(n))

    # Score both ways to see the difference
    result_still_halved = score_with_boss_disabled(state, play_indices, boss_disabled=False)
    result_disabled = score_with_boss_disabled(state, play_indices, boss_disabled=True)

    detail_halved = result_still_halved["detail"]
    detail_disabled = result_disabled["detail"]

    pre_chips = state.get("round", {}).get("chips", 0)
    print(f"  Hand: {result_disabled['hand_name']}")
    print(f"  Cards: {', '.join(fmt_card(c) for c in result_disabled['played'])}")
    print(f"  Estimate WITH halving (bug): {detail_halved['total']}  (base {detail_halved['base_chips']}/{detail_halved['base_mult']})")
    print(f"  Estimate WITHOUT halving (correct): {detail_disabled['total']}  (base {detail_disabled['base_chips']}/{detail_disabled['base_mult']})")

    # Play the hand
    jokers_pre = state.get("jokers", {}).get("cards", [])
    try:
        new_state = client.call("play", {"cards": play_indices})
    except APIError as e:
        print(f"  Play failed: {e.message}")
        return None

    post_chips = new_state.get("round", {}).get("chips", 0)
    actual_h2 = post_chips - pre_chips

    diff_halved = actual_h2 - detail_halved["total"]
    diff_disabled = actual_h2 - detail_disabled["total"]

    print(f"  Actual: {actual_h2}")
    print(f"  vs halved estimate:  diff={diff_halved:+d}  {'MATCH' if diff_halved == 0 else 'MISMATCH'}")
    print(f"  vs disabled estimate: diff={diff_disabled:+d}  {'MATCH' if diff_disabled == 0 else 'MISMATCH'}")

    if diff_disabled == 0:
        print(f"\n  *** CONFIRMED: After Luchador sell, Flint halving is DISABLED. ***")
        print(f"  *** The bot should NOT apply flint_halve_hand_levels after sell. ***")
    elif diff_halved == 0:
        print(f"\n  *** UNEXPECTED: After Luchador sell, Flint halving is STILL ACTIVE. ***")
        print(f"  *** The Lua disable() may not actually bypass modify_hand()? ***")
    else:
        print(f"\n  *** NEITHER estimate matched — something else is going on. ***")
        print(f"  *** Need to examine full scoring breakdown. ***")

    # Full scoring dump for hand #2
    jokers_post = new_state.get("jokers", {}).get("cards", [])
    print("\n  --- Hand #2 full detail (disabled estimate) ---")
    dump_hand_detail(
        detail_disabled, result_disabled["played"], result_disabled["held"],
        result_disabled["scoring"], jokers_pre, state,
        pre_chips, actual_h2, prefix="    ")
    dump_joker_diff(jokers_pre, jokers_post, prefix="    ")

    # --- Summary ---
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"  Hand #1 (Flint active, Luchador present):")
    print(f"    Est={detail_active['total']}  Actual={actual_h1}  Diff={diff_h1:+d}  {'MATCH' if diff_h1 == 0 else 'MISMATCH'}")
    print(f"  Hand #2 (Flint should be disabled after Luchador sell):")
    print(f"    Est (with halving) ={detail_halved['total']}  Diff={diff_halved:+d}")
    print(f"    Est (no halving)   ={detail_disabled['total']}  Diff={diff_disabled:+d}")
    print(f"    Actual={actual_h2}")

    return {
        "h1_match": diff_h1 == 0,
        "h2_disabled_match": diff_disabled == 0,
        "h2_halved_match": diff_halved == 0,
        "h1_diff": diff_h1,
        "h2_diff_disabled": diff_disabled,
        "h2_diff_halved": diff_halved,
    }


# ---------------------------------------------------------------------------
# Engine mode — replicate the production bot loop to trigger the bug
# ---------------------------------------------------------------------------

def run_engine_mode_test(client, seed="FLINTLUCH2"):
    """Run the real RuleEngine against The Flint with Luchador.

    This replicates the production flow where state flows from action responses
    and _boss_disabled gets lost on the next client.call().
    """
    from balatro_bot.engine import RuleEngine

    print("\n" + "=" * 70)
    print("TEST: Engine mode — production bot loop replication")
    print("=" * 70)

    # Setup
    state = setup_game(client, seed)
    set_ante(client, 2)
    try:
        client.call("set", {"money": 999999})
    except APIError:
        pass

    state = advance_to_boss_select(client, target_ante=2)
    gs = state.get("state", "")
    if gs == "BLIND_SELECT":
        force_boss(client, "The Flint")
        client.call("select")
        time.sleep(0.5)

    state = wait_for_state(client, {"SELECTING_HAND"})
    blind_name, _ = get_current_blind(state)
    if blind_name != "The Flint":
        print(f"  ABORT: Expected The Flint, got {blind_name}")
        return None

    # Clear and inject
    for _ in range(state.get("jokers", {}).get("count", 0)):
        try:
            client.call("sell", {"joker": 0})
        except APIError:
            pass

    inject_jokers(client, ["j_green_joker", "j_luchador"])
    try:
        client.call("set", {"hands": 6, "discards": 3})
    except APIError:
        pass

    # Run the engine — mirroring bot.py's main loop
    engine = RuleEngine()
    state = client.call("gamestate")
    prev_joker_keys = {j.get("key") for j in state.get("jokers", {}).get("cards", [])}
    boss_disabled = False
    results = []

    print("\n  --- Engine loop ---")
    for step in range(30):
        gs = state.get("state", "")
        if gs != "SELECTING_HAND":
            print(f"  Step {step}: state={gs}, stopping")
            break

        cur_blind, _ = get_current_blind(state)
        if cur_blind != "The Flint":
            print(f"  Step {step}: blind={cur_blind}, stopping")
            break

        action = engine.decide(state)
        if action is None:
            time.sleep(0.15)
            state = client.call("gamestate")
            continue

        method, params = action.to_rpc()

        # Pre-play snapshot (like bot.py)
        if method == "play":
            pre_chips = state.get("round", {}).get("chips", 0)
            snap_hand_levels = state.get("hands", {})
            # BUG CHECK: Does the bot check _boss_disabled before halving?
            for b in state.get("blinds", {}).values():
                if isinstance(b, dict) and b.get("status") == "CURRENT" and b.get("name") == "The Flint":
                    if not boss_disabled:
                        snap_hand_levels = flint_halve_hand_levels(snap_hand_levels)
                        print(f"    [h{len(results)+1}] Applying Flint halving (boss_disabled={boss_disabled})")
                    else:
                        print(f"    [h{len(results)+1}] SKIPPING Flint halving (boss_disabled={boss_disabled})")
                    break

        print(f"  Step {step}: {method} (reason={getattr(action, 'reason', '')[:60]})")

        try:
            state = client.call(method, params)  # ← This replaces state, losing _boss_disabled
        except APIError as e:
            print(f"    Action failed: {e.message}")
            state = client.call("gamestate")
            continue

        if method == "play":
            post_chips = state.get("round", {}).get("chips", 0)
            actual = post_chips - pre_chips
            # Score with our tracked boss_disabled
            hand_cards_snap = state.get("hand", {}).get("cards", [])
            # (We'd need the pre-play hand cards for accurate scoring — simplified here)
            print(f"    actual_delta={actual}  boss_disabled={boss_disabled}")
            results.append({
                "actual": actual,
                "boss_disabled": boss_disabled,
                "step": step,
            })

        # Detect Luchador sell (mirrors bot.py:800-804)
        cur_joker_keys = {j.get("key") for j in state.get("jokers", {}).get("cards", [])}
        if "j_luchador" in prev_joker_keys and "j_luchador" not in cur_joker_keys:
            boss_disabled = True
            print(f"    *** Luchador sold — boss_disabled=True ***")
            # In the real bot, this sets state["_boss_disabled"] = True
            # But the NEXT client.call() will replace state and lose it!
            state["_boss_disabled"] = True

        # Check if _boss_disabled survived from last iteration
        if step > 0:
            still_has_flag = state.get("_boss_disabled", False)
            print(f"    _boss_disabled in state dict: {still_has_flag}  (our tracking: {boss_disabled})")

        prev_joker_keys = cur_joker_keys
        time.sleep(0.15)

    return results


def main():
    parser = argparse.ArgumentParser(description="Flint + Luchador integration test")
    parser.add_argument("--port", type=int, default=TEST_PORT)
    parser.add_argument("--seed", type=str, default="FLINTLUCH")
    parser.add_argument("--start-server", action="store_true")
    parser.add_argument("--engine-only", action="store_true",
                        help="Only run engine mode test")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    server_proc = None
    if args.start_server:
        client, server_proc = ensure_server(args.port)
    else:
        client = BalatroClient(port=args.port)
        try:
            client.call("health")
        except Exception as e:
            print(f"No server on port {args.port}: {e}")
            print("Use --start-server to auto-launch, or start one manually.")
            sys.exit(1)

    result = None
    engine_result = None
    try:
        if not args.engine_only:
            # Test 1: Controlled Flint + Luchador lifecycle
            result = run_flint_luchador_test(client, seed=args.seed + "1")

        # Test 2: Engine mode (production loop replication)
        engine_result = run_engine_mode_test(client, seed=args.seed + "2")

        # --- Mismatch assertions ---
        failures = []

        if not args.engine_only and result is not None:
            if result["h1_diff"] != 0:
                failures.append(f"Hand #1 (Flint active): diff={result['h1_diff']:+d}")
            if result["h2_diff_disabled"] != 0:
                failures.append(f"Hand #2 (Flint disabled): diff={result['h2_diff_disabled']:+d}")

        # Engine mode doesn't compute estimate diffs inline, so a None result
        # (test aborted) is the main failure signal from that test.

        if result is None and not args.engine_only:
            failures.append("run_flint_luchador_test aborted (returned None)")
        if engine_result is None:
            failures.append("run_engine_mode_test aborted (returned None)")

        if failures:
            print(f"\nFAILED: {len(failures)} failure(s)")
            for f in failures:
                print(f"  - {f}")
            sys.exit(1)
        print("\nPASSED: all scores matched")

    finally:
        if server_proc:
            stop_server(server_proc)


if __name__ == "__main__":
    main()
