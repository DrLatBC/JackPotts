"""Integration tests: The Flint halving accuracy and Vampire enhancement stripping.

Flint: Verifies that pre-halving hand levels matches the game's Blind:modify_hand().
  Tests with per-card jokers (Greedy, Photograph), editions (Foil, Steel), and
  accumulator jokers (Ride the Bus) that appeared in batch 073 mismatches.

Vampire: Verifies enhancement stripping in the 'before' phase, and that joker
  order doesn't affect the outcome (Vampire fires in 'before', per-card effects
  fire in card scoring, so the stripping always precedes card-level effects).

Usage:
    python tests/integration/test_flint_vampire_v2.py --start-server
    python tests/integration/test_flint_vampire_v2.py --port 12346
"""

import argparse
import logging
import math
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "support"))
_venv_sp = os.path.join(os.path.dirname(__file__), "..", "..", ".venv", "Lib", "site-packages")
if os.path.isdir(_venv_sp) and _venv_sp not in sys.path:
    sys.path.insert(0, os.path.abspath(_venv_sp))

from balatrobot.cli.client import BalatroClient, APIError
from balatro_bot.domain.scoring.classify import classify_hand, _scoring_cards_for
from balatro_bot.domain.scoring.estimate import score_hand_detailed
from balatro_bot.domain.scoring.base import flint_halve_hand_levels
from balatro_bot.cards import card_chip_value, _modifier
from balatro_bot.joker_effects.parsers import parse_effect_value, _ability, _ab_xmult

from harness import wait_for_state

# Lazy-imported: harness eagerly validates config paths at import time,
# which fails if BALATRO_EXE isn't set.  Only needed for --start-server.
# from harness import TEST_PORT, ensure_server, stop_server


# =========================================================================
# Helpers
# =========================================================================

def get_current_blind(state):
    for b in state.get("blinds", {}).values():
        if isinstance(b, dict) and b.get("status") == "CURRENT":
            return b.get("name", ""), b.get("key", "")
    return "", ""


def _beat_blind_fast(client, state):
    try:
        client.call("set", {"chips": 999999})
    except APIError:
        pass
    hc = state.get("hand", {}).get("cards", [])
    if hc:
        client.call("play", {"cards": list(range(min(5, len(hc))))})
    time.sleep(0.5)


def _is_boss_blind_select(state):
    blinds = state.get("blinds", {})
    small_status = blinds.get("small", {}).get("status", "") if isinstance(blinds.get("small"), dict) else ""
    big_status = blinds.get("big", {}).get("status", "") if isinstance(blinds.get("big"), dict) else ""
    return small_status == "DEFEATED" and big_status == "DEFEATED"


def _advance_to_boss(client, boss_blind_key, max_tries=50):
    gs = ""
    for _ in range(max_tries):
        state = client.call("gamestate")
        gs = state.get("state", "")
        if gs == "SELECTING_HAND":
            blind_name, _ = get_current_blind(state)
            if blind_name not in ("Small Blind", "Big Blind"):
                return state
            _beat_blind_fast(client, state)
        elif gs == "ROUND_EVAL":
            client.call("cash_out")
            time.sleep(0.3)
        elif gs == "SHOP":
            client.call("next_round")
            time.sleep(0.3)
        elif gs == "BLIND_SELECT":
            if _is_boss_blind_select(state):
                try:
                    client.call("set", {"blind": boss_blind_key})
                except APIError as e:
                    print(f"  set blind failed: {e.message}")
            client.call("select")
            time.sleep(0.3)
        elif gs in ("HAND_PLAYED", "DRAW_TO_HAND", "NEW_ROUND"):
            time.sleep(0.3)
        else:
            time.sleep(0.3)
    raise TimeoutError(f"Never reached boss blind, stuck in {gs}")


def setup_round(client, seed, joker_keys=None, card_configs=None,
                boss_blind_key=None, ante=None):
    """Start a game, optionally advance to a boss blind, inject jokers + cards."""
    try:
        client.call("menu")
    except APIError:
        pass
    time.sleep(0.5)
    client.call("start", {"deck": "RED", "stake": "WHITE", "seed": seed})
    state = wait_for_state(client, ["SELECTING_HAND"])

    if ante:
        try:
            client.call("set", {"ante": ante})
        except APIError as e:
            print(f"  set ante failed: {e.message}")

    if boss_blind_key:
        _beat_blind_fast(client, state)
        state = _advance_to_boss(client, boss_blind_key)
        blind_name, _ = get_current_blind(state)
        print(f"  At boss blind: {blind_name}")

    # Clear existing jokers
    for i in range(state.get("jokers", {}).get("count", 0)):
        try:
            client.call("sell", {"joker": 0})
        except APIError:
            pass

    # Discard existing hand to make room for injected cards
    for _ in range(3):
        state = client.call("gamestate")
        hc = state.get("hand", {}).get("cards", [])
        if hc:
            try:
                client.call("discard", {"cards": list(range(min(len(hc), 5)))})
                time.sleep(0.2)
            except APIError:
                break

    # Add jokers (supports both string keys and dicts with edition/etc)
    if joker_keys:
        for jk in joker_keys:
            params = {"key": jk} if isinstance(jk, str) else jk
            try:
                client.call("add", params)
            except APIError as e:
                print(f"  FAILED joker {params}: {e.message}")

    # Add controlled hand cards
    if card_configs:
        for cfg in card_configs:
            params = {"key": cfg["key"]}
            for k in ("edition", "enhancement", "seal"):
                if k in cfg:
                    params[k] = cfg[k]
            try:
                client.call("add", params)
            except APIError as e:
                print(f"  FAILED card {cfg['key']}: {e.message}")

    time.sleep(0.3)
    return client.call("gamestate")


def play_and_compare(client, state, play_count=5, label="", blind_name_override=None):
    """Play the last N cards and compare bot estimate vs game actual.

    Applies Flint halving to hand levels when The Flint is detected,
    matching what the bot does in bot.py.
    """
    jokers = state.get("jokers", {}).get("cards", [])
    hand_cards = state.get("hand", {}).get("cards", [])
    hand_levels = state.get("hands", {})

    blind_name = blind_name_override
    if not blind_name:
        blind_name, _ = get_current_blind(state)

    # Apply Flint halving (same as bot.py line 728-733)
    if blind_name == "The Flint":
        hand_levels = flint_halve_hand_levels(hand_levels)

    play_indices = list(range(len(hand_cards) - play_count, len(hand_cards)))
    played = [hand_cards[i] for i in play_indices]
    held = [c for j, c in enumerate(hand_cards) if j not in set(play_indices)]

    joker_key_set = {j.get("key") for j in jokers}
    hand_name = classify_hand(played)
    scoring = played if "j_splash" in joker_key_set else _scoring_cards_for(hand_name, played)

    detail = score_hand_detailed(
        hand_name, scoring,
        hand_levels=hand_levels,
        jokers=jokers,
        played_cards=played,
        held_cards=held,
        money=state.get("money", 0),
        discards_left=state.get("round", {}).get("discards_left", 0),
        hands_left=state.get("round", {}).get("hands_left", 4),
        joker_limit=state.get("jokers", {}).get("limit", 5),
        blind_name=blind_name,
    )

    pre_chips = state.get("round", {}).get("chips", 0)
    print(f"  Blind: {blind_name}")
    print(f"  Hand: {hand_name}")
    print(f"  Playing: {[c.get('label','?') for c in played]}")
    print(f"  Jokers: {[j.get('key','?') for j in jokers]}")
    print(f"  Base: {detail['base_chips']}/{detail['base_mult']}")
    print(f"  Pre-joker: {detail['pre_joker_chips']}/{detail['pre_joker_mult']:.2f}")
    for entry in detail.get("joker_contributions", []):
        jlabel, dc, dm = entry[0], entry[1], entry[2]
        xm = entry[3] if len(entry) > 3 else 1.0
        parts = []
        if dc: parts.append(f"+{dc:.0f}c")
        if xm > 1.01 or xm < 0.99: parts.append(f"x{xm:.2f}")
        elif dm: parts.append(f"+{dm:.1f}m")
        if parts: print(f"    {jlabel}: {', '.join(parts)}")
    print(f"  Post-joker: {detail['post_joker_chips']}/{detail['post_joker_mult']:.2f}")
    print(f"  Est: {detail['total']}")

    try:
        new_state = client.call("play", {"cards": play_indices})
    except APIError as e:
        print(f"  Play failed: {e.message}")
        return None

    post_chips = new_state.get("round", {}).get("chips", 0)
    actual = post_chips - pre_chips
    diff = actual - detail["total"]

    status = "MATCH" if diff == 0 else f"MISMATCH({diff:+d})"
    print(f"  Actual: {actual}  Diff: {diff:+d}  {status}")

    return {"label": label, "est": detail["total"], "actual": actual, "diff": diff,
            "blind": blind_name, "hand": hand_name}


# =========================================================================
# FLINT TESTS
# =========================================================================

def run_flint_tests(client):
    """Test Flint halving with joker combos that appeared in batch 073 mismatches."""
    print("\n" + "=" * 60)
    print("FLINT TESTS — halving accuracy with per-card jokers")
    print("=" * 60)
    results = []

    # -----------------------------------------------------------------
    # Test 1: Flint + bare Pair (baseline)
    # Two Pair level 1: base 20/2 → halved 10/1
    # -----------------------------------------------------------------
    print(f"\n--- Flint bare Two Pair ---")
    state = setup_round(client, "FV1", boss_blind_key="bl_flint", ante=2,
        card_configs=[
            {"key": "S_A"}, {"key": "H_A"},
            {"key": "D_9"}, {"key": "C_9"}, {"key": "S_3"},
        ])
    if state:
        r = play_and_compare(client, state, label="Flint Two Pair bare")
        if r: results.append(r)

    # -----------------------------------------------------------------
    # Test 2: Flint + Foil Joker (Juggler) + Greedy Joker (Foil)
    # Reproduces the batch 073 port 12354 Flint mismatch pattern.
    # -----------------------------------------------------------------
    print(f"\n--- Flint + Foil Juggler + Foil Greedy + Ride the Bus ---")
    state = setup_round(client, "FV2", boss_blind_key="bl_flint", ante=2,
        joker_keys=[
            {"key": "j_juggler", "edition": "FOIL"},
            "j_ride_the_bus",
            {"key": "j_greedy_joker", "edition": "FOIL"},
        ],
        card_configs=[
            {"key": "H_A"}, {"key": "C_A"},
            {"key": "D_T"}, {"key": "H_T"}, {"key": "S_4"},
        ])
    if state:
        r = play_and_compare(client, state, label="Flint Two Pair + Foils + Ride")
        if r: results.append(r)

    # -----------------------------------------------------------------
    # Test 3: Flint + Photograph (x2 first face card)
    # Full House with Kings (face cards) — Photograph should fire.
    # -----------------------------------------------------------------
    print(f"\n--- Flint + Photograph: Full House with Kings ---")
    state = setup_round(client, "FV3", boss_blind_key="bl_flint", ante=2,
        joker_keys=["j_photograph"],
        card_configs=[
            {"key": "S_K"}, {"key": "H_K"}, {"key": "D_K"},
            {"key": "S_7"}, {"key": "H_7"},
        ])
    if state:
        r = play_and_compare(client, state, label="Flint FH + Photograph")
        if r: results.append(r)

    # -----------------------------------------------------------------
    # Test 4: Flint + Steel card in scoring hand
    # Steel on a played card gives x1.5 per trigger during scoring.
    # -----------------------------------------------------------------
    print(f"\n--- Flint + Steel scored card ---")
    state = setup_round(client, "FV4", boss_blind_key="bl_flint", ante=2,
        card_configs=[
            {"key": "S_J"}, {"key": "D_T", "enhancement": "STEEL"},
            {"key": "H_9"}, {"key": "C_8"}, {"key": "S_7"},
        ])
    if state:
        r = play_and_compare(client, state, label="Flint Straight + Steel")
        if r: results.append(r)

    # -----------------------------------------------------------------
    # Test 5: Flint + MULT enhanced card
    # MULT card gives +4 mult when scored.
    # -----------------------------------------------------------------
    print(f"\n--- Flint + MULT card ---")
    state = setup_round(client, "FV5", boss_blind_key="bl_flint", ante=2,
        card_configs=[
            {"key": "S_K"}, {"key": "H_K"},
            {"key": "D_3", "enhancement": "MULT"},
            {"key": "C_4"}, {"key": "S_5"},
        ])
    if state:
        r = play_and_compare(client, state, label="Flint Pair + MULT card")
        if r: results.append(r)

    # -----------------------------------------------------------------
    # Test 6: Flint + Cavendish (xmult joker)
    # Tests that xmult interacts correctly with halved base.
    # -----------------------------------------------------------------
    print(f"\n--- Flint + Cavendish: Three of a Kind ---")
    state = setup_round(client, "FV6", boss_blind_key="bl_flint", ante=2,
        joker_keys=["j_cavendish"],
        card_configs=[
            {"key": "S_K"}, {"key": "H_K"}, {"key": "D_K"},
            {"key": "C_4"}, {"key": "S_5"},
        ])
    if state:
        r = play_and_compare(client, state, label="Flint 3oK + Cavendish")
        if r: results.append(r)

    # -----------------------------------------------------------------
    # Test 7: Flint + Even Steven + leveled Pair
    # Pair level 2 (25/3) halved (13/2) with even cards for Even Steven.
    # -----------------------------------------------------------------
    print(f"\n--- Flint + Even Steven: Pair of 6s ---")
    state = setup_round(client, "FV7", boss_blind_key="bl_flint", ante=2,
        joker_keys=["j_even_steven"],
        card_configs=[
            {"key": "H_6"}, {"key": "D_6"},
            {"key": "S_5"}, {"key": "C_3"}, {"key": "H_7"},
        ])
    if state:
        r = play_and_compare(client, state, label="Flint Pair 6s + Even Steven")
        if r: results.append(r)

    # -----------------------------------------------------------------
    # Test 8: Flint + Glass card (x2 xmult per trigger)
    # -----------------------------------------------------------------
    print(f"\n--- Flint + Glass card ---")
    state = setup_round(client, "FV8", boss_blind_key="bl_flint", ante=2,
        card_configs=[
            {"key": "S_K", "enhancement": "GLASS"}, {"key": "H_K"},
            {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"},
        ])
    if state:
        r = play_and_compare(client, state, label="Flint Pair + Glass")
        if r: results.append(r)

    return results


# =========================================================================
# VAMPIRE TESTS — enhancement stripping order
# =========================================================================

def run_vampire_tests(client):
    """Test Vampire's enhancement stripping in the 'before' phase.

    Vampire strips enhancements before card scoring, so per-card enhancement
    effects (MULT +4, Glass x2, Steel x1.5) should NOT fire on stripped cards.
    The key test: reorder Vampire relative to other jokers and verify the
    score stays the same, since Vampire fires in 'before' regardless of
    joker position.
    """
    print("\n" + "=" * 60)
    print("VAMPIRE TESTS — enhancement stripping and joker order")
    print("=" * 60)
    results = []

    # Common hands for testing
    mult_pair = [
        {"key": "S_K", "enhancement": "MULT"}, {"key": "H_K", "enhancement": "MULT"},
        {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"},
    ]
    glass_pair = [
        {"key": "S_K", "enhancement": "GLASS"}, {"key": "H_K", "enhancement": "GLASS"},
        {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"},
    ]
    steel_straight = [
        {"key": "D_J"}, {"key": "S_T", "enhancement": "STEEL"},
        {"key": "H_9"}, {"key": "C_8"}, {"key": "S_7"},
    ]
    wild_flush = [
        {"key": "H_A"}, {"key": "H_K"},
        {"key": "S_9", "enhancement": "WILD"}, {"key": "H_5"}, {"key": "H_2"},
    ]

    # -----------------------------------------------------------------
    # Test 1: Vampire alone + MULT cards
    # Vampire strips MULT, so no +4 mult from cards.
    # Vampire gains xmult from 2 stripped cards.
    # -----------------------------------------------------------------
    print(f"\n--- Vampire + 2 MULT Kings (Pair) ---")
    state = setup_round(client, "VP1",
        joker_keys=["j_vampire"],
        card_configs=mult_pair)
    if state:
        r = play_and_compare(client, state, label="Vampire + MULT pair")
        if r: results.append(r)

    # -----------------------------------------------------------------
    # Test 2: Vampire + MULT cards — Vampire FIRST in joker order
    # Should match Test 1 scoring (before phase fires regardless of position).
    # -----------------------------------------------------------------
    print(f"\n--- Vampire FIRST + Joker + MULT pair ---")
    state = setup_round(client, "VP2",
        joker_keys=["j_vampire", "j_joker"],
        card_configs=mult_pair)
    if state:
        r = play_and_compare(client, state, label="Vampire 1st + Joker + MULT")
        if r: results.append(r)

    # -----------------------------------------------------------------
    # Test 3: Vampire LAST + MULT cards
    # Should produce same scoring as Test 2 if before-phase is order-independent.
    # -----------------------------------------------------------------
    print(f"\n--- Joker + Vampire LAST + MULT pair ---")
    state = setup_round(client, "VP3",
        joker_keys=["j_joker", "j_vampire"],
        card_configs=mult_pair)
    if state:
        r = play_and_compare(client, state, label="Joker + Vampire last + MULT")
        if r: results.append(r)

    # -----------------------------------------------------------------
    # Test 4: Vampire + Glass cards
    # Vampire strips Glass, so no x2 xmult from Glass.
    # Vampire gains xmult from 2 stripped cards.
    # -----------------------------------------------------------------
    print(f"\n--- Vampire + 2 Glass Kings (Pair) ---")
    state = setup_round(client, "VP4",
        joker_keys=["j_vampire"],
        card_configs=glass_pair)
    if state:
        r = play_and_compare(client, state, label="Vampire + Glass pair")
        if r: results.append(r)

    # -----------------------------------------------------------------
    # Test 5: Vampire + Glass — Vampire first vs last
    # -----------------------------------------------------------------
    print(f"\n--- Vampire FIRST + Joker + Glass pair ---")
    state = setup_round(client, "VP5",
        joker_keys=["j_vampire", "j_joker"],
        card_configs=glass_pair)
    if state:
        r = play_and_compare(client, state, label="Vampire 1st + Joker + Glass")
        if r: results.append(r)

    print(f"\n--- Joker + Vampire LAST + Glass pair ---")
    state = setup_round(client, "VP6",
        joker_keys=["j_joker", "j_vampire"],
        card_configs=glass_pair)
    if state:
        r = play_and_compare(client, state, label="Joker + Vampire last + Glass")
        if r: results.append(r)

    # -----------------------------------------------------------------
    # Test 7: Vampire + Steel scored card
    # Vampire strips Steel, so no x1.5 from Steel.
    # -----------------------------------------------------------------
    print(f"\n--- Vampire + Steel in Straight ---")
    state = setup_round(client, "VP7",
        joker_keys=["j_vampire"],
        card_configs=steel_straight)
    if state:
        r = play_and_compare(client, state, label="Vampire + Steel straight")
        if r: results.append(r)

    # -----------------------------------------------------------------
    # Test 8: Vampire + Wild card + Flower Pot
    # Wild card counts as all suits for Flower Pot.
    # Vampire strips Wild BEFORE Flower Pot checks suits.
    # If stripped: Flower Pot may not see all 4 suits → no x3 bonus.
    # -----------------------------------------------------------------
    print(f"\n--- Vampire + Wild + Flower Pot (Vampire FIRST) ---")
    state = setup_round(client, "VP8a",
        joker_keys=["j_vampire", "j_flower_pot"],
        card_configs=wild_flush)
    if state:
        r = play_and_compare(client, state, label="Vampire 1st + Wild + FlowerPot")
        if r: results.append(r)

    print(f"\n--- Flower Pot + Vampire LAST + Wild ---")
    state = setup_round(client, "VP8b",
        joker_keys=["j_flower_pot", "j_vampire"],
        card_configs=wild_flush)
    if state:
        r = play_and_compare(client, state, label="FlowerPot + Vampire last + Wild")
        if r: results.append(r)

    # -----------------------------------------------------------------
    # Test 9: Vampire + Greedy Joker + MULT Diamond
    # Greedy fires per-card on Diamond suit (unaffected by Vampire strip).
    # MULT enhancement is stripped, so no +4 mult. Greedy still fires.
    # -----------------------------------------------------------------
    print(f"\n--- Vampire + Greedy + MULT Diamond pair ---")
    state = setup_round(client, "VP9",
        joker_keys=["j_vampire", "j_greedy_joker"],
        card_configs=[
            {"key": "D_K", "enhancement": "MULT"}, {"key": "H_K"},
            {"key": "S_3"}, {"key": "C_4"}, {"key": "D_5"},
        ])
    if state:
        r = play_and_compare(client, state, label="Vampire + Greedy + MULT Dia")
        if r: results.append(r)

    # -----------------------------------------------------------------
    # Test 10: Vampire with no enhanced cards (baseline — just xmult)
    # Verify Vampire applies its current xmult even when no cards stripped.
    # -----------------------------------------------------------------
    print(f"\n--- Vampire with no enhancements (baseline xmult) ---")
    state = setup_round(client, "VP10",
        joker_keys=["j_vampire"],
        card_configs=[
            {"key": "S_K"}, {"key": "H_K"},
            {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"},
        ])
    if state:
        r = play_and_compare(client, state, label="Vampire bare (no enh)")
        if r: results.append(r)

    return results


# =========================================================================
# Main
# =========================================================================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=None,
                        help="Server port (default: auto with --start-server, 12346 without)")
    parser.add_argument("--start-server", action="store_true",
                        help="Auto-start a balatrobot server (killed on exit)")
    parser.add_argument("--skip-flint", action="store_true")
    parser.add_argument("--skip-vampire", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    server_proc = None

    if args.start_server:
        from harness import TEST_PORT, ensure_server, stop_server as _stop
        port = args.port or TEST_PORT
        client, server_proc = ensure_server(port)
    else:
        _stop = lambda p: None
        port = args.port or 12346
        client = BalatroClient(port=port)
        try:
            client.call("health")
        except Exception as e:
            print(f"No server on port {port}: {e}")
            sys.exit(1)

    all_results = []

    try:
        if not args.skip_flint:
            all_results.extend(run_flint_tests(client))
        else:
            print("\n  [Skipping Flint tests]")

        if not args.skip_vampire:
            all_results.extend(run_vampire_tests(client))
        else:
            print("\n  [Skipping Vampire tests]")

        # ===================================================================
        print(f"\n\n{'=' * 60}")
        print("SUMMARY")
        print(f"{'=' * 60}")
        matches = 0
        mismatches = 0
        for r in all_results:
            if r is None:
                continue
            status = "MATCH" if r["diff"] == 0 else f"MISMATCH({r['diff']:+d})"
            if r["diff"] == 0:
                matches += 1
            else:
                mismatches += 1
            print(f"  {r['label']:50s} est={r['est']:>6d} actual={r['actual']:>6d} {status}")

        total = matches + mismatches
        print(f"\n  Total: {matches} match, {mismatches} mismatch out of {total}")

        # Vampire order check: compare paired tests
        order_pairs = [
            ("Vampire 1st + Joker + MULT", "Joker + Vampire last + MULT"),
            ("Vampire 1st + Joker + Glass", "Joker + Vampire last + Glass"),
            ("Vampire 1st + Wild + FlowerPot", "FlowerPot + Vampire last + Wild"),
        ]
        print(f"\n  Vampire order-independence checks:")
        by_label = {r["label"]: r for r in all_results if r}
        for a_label, b_label in order_pairs:
            a = by_label.get(a_label)
            b = by_label.get(b_label)
            if a and b:
                same = a["actual"] == b["actual"]
                print(f"    {a_label} vs {b_label}: "
                      f"actual={a['actual']} vs {b['actual']} "
                      f"{'SAME' if same else 'DIFFERENT'}")

    finally:
        if server_proc:
            _stop(server_proc)

    assert mismatches == 0, f"{mismatches} scoring mismatch(es) — see SUMMARY above"


if __name__ == "__main__":
    main()
