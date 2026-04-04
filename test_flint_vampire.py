"""Integration tests for scoring accuracy: rounding and The Flint boss blind.

Rounding: bot uses round() but game uses math.floor() for final score.
  When chips * mult has a .5 fractional part (e.g., from x1.5 Polychrome),
  round() rounds to even (536) but floor() truncates (535). They differ
  when the integer part is odd.

The Flint: halves base chips and mult at scoring time via Blind:modify_hand().
  The API sends un-halved hand levels, so the bot must apply the halving itself.
  Formula: floor(chips*0.5+0.5) / max(floor(mult*0.5+0.5), 1)

Usage:
    python test_flint_vampire.py [--port PORT]
"""

import argparse
import logging
import math
import sys
import time

sys.path.insert(0, "src")

from balatrobot.cli.client import BalatroClient, APIError
from balatro_bot.domain.scoring.classify import classify_hand, _scoring_cards_for
from balatro_bot.domain.scoring.estimate import score_hand_detailed
from balatro_bot.cards import card_chip_value, _modifier
from balatro_bot.joker_effects.parsers import parse_effect_value, _ability, _ab_xmult


def wait_for_state(client, target_states, max_tries=30):
    for _ in range(max_tries):
        state = client.call("gamestate")
        gs = state.get("state", "")
        if gs in target_states:
            return state
        if gs == "BLIND_SELECT":
            client.call("select")
            time.sleep(0.3)
            continue
        if gs == "SHOP":
            client.call("next_round")
            time.sleep(0.3)
            continue
        if gs in ("HAND_PLAYED", "DRAW_TO_HAND", "NEW_ROUND", "ROUND_EVAL"):
            time.sleep(0.3)
            continue
        time.sleep(0.3)
    raise TimeoutError(f"Never reached {target_states}, stuck in {state.get('state')}")


def setup_game(client, seed, joker_keys=None, card_configs=None):
    try:
        client.call("menu")
    except APIError:
        pass
    time.sleep(0.5)
    client.call("start", {"deck": "RED", "stake": "WHITE", "seed": seed})
    state = wait_for_state(client, ["SELECTING_HAND"])

    for i in range(state.get("jokers", {}).get("count", 0)):
        try:
            client.call("sell", {"joker": 0})
        except APIError:
            pass

    for _ in range(2):
        state = client.call("gamestate")
        hc = state.get("hand", {}).get("cards", [])
        if hc:
            try:
                client.call("discard", {"cards": list(range(min(len(hc), 5)))})
                time.sleep(0.2)
            except APIError:
                pass

    if joker_keys:
        for jk in joker_keys:
            params = {"key": jk} if isinstance(jk, str) else jk
            try:
                client.call("add", params)
            except APIError as e:
                print(f"  FAILED joker {params}: {e.message}")

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


def get_current_blind(state):
    """Return (name, key) of the CURRENT blind."""
    for b in state.get("blinds", {}).values():
        if isinstance(b, dict) and b.get("status") == "CURRENT":
            return b.get("name", ""), b.get("key", "")
    return "", ""


def dump_jokers(jokers):
    for j in jokers:
        key = j.get("key", "?")
        ab = j.get("value", {}).get("ability", {})
        effect = j.get("value", {}).get("effect", "")
        parsed = parse_effect_value(effect)
        print(f"    {key:20s}")
        print(f"      ability  = {ab}")
        print(f"      effect   = {effect[:140]}")
        print(f"      parsed   = {parsed}")


def play_and_compare(client, state, play_count=5, label=""):
    """Play the last N cards in hand and compare bot estimate vs actual."""
    jokers = state.get("jokers", {}).get("cards", [])
    hand_cards = state.get("hand", {}).get("cards", [])
    hand_levels = state.get("hands", {})

    blind_name, blind_key = get_current_blind(state)

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
    )

    pre_chips = state.get("round", {}).get("chips", 0)
    print(f"  Blind: {blind_name} (key={blind_key})")
    print(f"  Hand: {hand_name}")
    print(f"  Playing: {[c.get('label','?') for c in played]}")
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

    # Show both round() and floor() estimates
    raw = detail['post_joker_chips'] * detail['post_joker_mult']
    est_round = round(raw)
    est_floor = math.floor(raw)
    print(f"  Raw: {raw:.1f}  round={est_round}  floor={est_floor}  bot_est={detail['total']}")

    # Flint manual estimate
    if blind_name == "The Flint":
        bc = detail["base_chips"]
        bm = detail["base_mult"]
        hc = max(math.floor(bc * 0.5 + 0.5), 0)
        hm = max(math.floor(bm * 0.5 + 0.5), 1)
        print(f"  Flint-halved base: {hc}/{hm} (from {bc}/{bm})")

    try:
        new_state = client.call("play", {"cards": play_indices})
    except APIError as e:
        print(f"  Play failed: {e.message}")
        return None

    post_chips = new_state.get("round", {}).get("chips", 0)
    actual = post_chips - pre_chips
    diff = actual - detail["total"]

    print(f"  Actual: {actual}  Diff: {diff:+d}  {'MATCH' if diff == 0 else 'MISMATCH'}")

    return {"label": label, "est": detail["total"], "actual": actual, "diff": diff,
            "blind": blind_name, "hand": hand_name,
            "est_floor": est_floor, "est_round": est_round}


# =========================================================================
# ROUNDING TESTS — round() vs math.floor()
# =========================================================================

def run_rounding_tests(client):
    """Test whether round() vs floor() causes ±1 mismatches.

    Strategy: use Polychrome (x1.5) on odd mult values to create X.5 products.
    Three of a Kind base mult = 3, + Joker +4 = 7 (odd), × 1.5 = 10.5.
    When chips * 10.5 = N.5 and N is odd, round→N+1 but floor→N → diff of -1.
    When N is even, banker's rounding agrees with floor → diff of 0.
    """
    print("\n" + "=" * 60)
    print("ROUNDING TESTS — round() vs math.floor()")
    print("=" * 60)

    results = []
    poly_joker = {"key": "j_joker", "edition": "POLYCHROME"}

    # ---------------------------------------------------------------
    # Test 1: Three of a Kind (3×7) + Polychrome Joker
    # chips=30+7+7+7=51, mult=(3+4)×1.5=10.5, raw=535.5
    # floor=535 (odd), round=536 → expect MISMATCH(-1)
    # ---------------------------------------------------------------
    print(f"\n--- 3oK(7s) + Poly Joker: 51×10.5=535.5 → floor=535, round=536 ---")
    state = setup_game(client, "RND1", joker_keys=[poly_joker],
        card_configs=[
            {"key": "S_7"}, {"key": "H_7"}, {"key": "D_7"},
            {"key": "C_4"}, {"key": "S_5"}
        ])
    dump_jokers(state.get("jokers", {}).get("cards", []))
    r = play_and_compare(client, state, label="3oK(7s)+Poly: 535.5 → floor≠round")
    if r: results.append(r)

    # ---------------------------------------------------------------
    # Test 2: Three of a Kind (3×3) + Polychrome Joker
    # chips=30+3+3+3=39, mult=10.5, raw=409.5
    # floor=409 (odd), round=410 → expect MISMATCH(-1)
    # ---------------------------------------------------------------
    print(f"\n--- 3oK(3s) + Poly Joker: 39×10.5=409.5 → floor=409, round=410 ---")
    state = setup_game(client, "RND2", joker_keys=[poly_joker],
        card_configs=[
            {"key": "S_3"}, {"key": "H_3"}, {"key": "D_3"},
            {"key": "C_4"}, {"key": "S_5"}
        ])
    r = play_and_compare(client, state, label="3oK(3s)+Poly: 409.5 → floor≠round")
    if r: results.append(r)

    # ---------------------------------------------------------------
    # Test 3: Three of a Kind (3×A) + Polychrome Joker
    # chips=30+11+11+11=63, mult=10.5, raw=661.5
    # floor=661 (odd), round=662 → expect MISMATCH(-1)
    # ---------------------------------------------------------------
    print(f"\n--- 3oK(As) + Poly Joker: 63×10.5=661.5 → floor=661, round=662 ---")
    state = setup_game(client, "RND3", joker_keys=[poly_joker],
        card_configs=[
            {"key": "S_A"}, {"key": "H_A"}, {"key": "D_A"},
            {"key": "C_4"}, {"key": "S_5"}
        ])
    r = play_and_compare(client, state, label="3oK(As)+Poly: 661.5 → floor≠round")
    if r: results.append(r)

    # ---------------------------------------------------------------
    # Test 4: Three of a Kind (3×5) + Polychrome Joker
    # chips=30+5+5+5=45, mult=10.5, raw=472.5
    # floor=472 (EVEN), round=472 → expect MATCH (banker's rounding agrees)
    # ---------------------------------------------------------------
    print(f"\n--- 3oK(5s) + Poly Joker: 45×10.5=472.5 → floor=round=472 ---")
    state = setup_game(client, "RND4", joker_keys=[poly_joker],
        card_configs=[
            {"key": "S_5"}, {"key": "H_5"}, {"key": "D_5"},
            {"key": "C_4"}, {"key": "S_6"}
        ])
    r = play_and_compare(client, state, label="3oK(5s)+Poly: 472.5 → floor=round")
    if r: results.append(r)

    # ---------------------------------------------------------------
    # Test 5: Pair (2×K) + Polychrome Joker — integer product
    # chips=10+10+10=30, mult=(2+4)×1.5=9.0, raw=270.0
    # floor=round=270 → expect MATCH
    # ---------------------------------------------------------------
    print(f"\n--- Pair(Ks) + Poly Joker: 30×9.0=270 → integer MATCH ---")
    state = setup_game(client, "RND5", joker_keys=[poly_joker],
        card_configs=[
            {"key": "S_K"}, {"key": "H_K"},
            {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"}
        ])
    r = play_and_compare(client, state, label="Pair(Ks)+Poly: 270 → integer")
    if r: results.append(r)

    # ---------------------------------------------------------------
    # Test 6: Three of a Kind (3×9) + Polychrome Joker
    # chips=30+9+9+9=57, mult=10.5, raw=598.5
    # floor=598 (EVEN), round=598 → expect MATCH
    # ---------------------------------------------------------------
    print(f"\n--- 3oK(9s) + Poly Joker: 57×10.5=598.5 → floor=round=598 ---")
    state = setup_game(client, "RND6", joker_keys=[poly_joker],
        card_configs=[
            {"key": "S_9"}, {"key": "H_9"}, {"key": "D_9"},
            {"key": "C_4"}, {"key": "S_5"}
        ])
    r = play_and_compare(client, state, label="3oK(9s)+Poly: 598.5 → floor=round")
    if r: results.append(r)

    # ---------------------------------------------------------------
    # Test 7: Full House (3K+2A) + Polychrome Joker — integer product
    # chips=40+10+10+10+11+11=92, mult=(4+4)×1.5=12.0, raw=1104
    # floor=round=1104 → expect MATCH
    # ---------------------------------------------------------------
    print(f"\n--- Full House(KKK/AA) + Poly Joker: 92×12=1104 → integer ---")
    state = setup_game(client, "RND7", joker_keys=[poly_joker],
        card_configs=[
            {"key": "S_K"}, {"key": "H_K"}, {"key": "D_K"},
            {"key": "S_A"}, {"key": "H_A"}
        ])
    r = play_and_compare(client, state, label="FH(KKK/AA)+Poly: 1104 → integer")
    if r: results.append(r)

    # ---------------------------------------------------------------
    # Test 8: Three of a Kind (3×T) + Polychrome Joker
    # chips=30+10+10+10=60, mult=10.5, raw=630.0
    # floor=round=630 → expect MATCH (integer despite x1.5)
    # ---------------------------------------------------------------
    print(f"\n--- 3oK(Ts) + Poly Joker: 60×10.5=630 → integer MATCH ---")
    state = setup_game(client, "RND8", joker_keys=[poly_joker],
        card_configs=[
            {"key": "S_T"}, {"key": "H_T"}, {"key": "D_T"},
            {"key": "C_4"}, {"key": "S_5"}
        ])
    r = play_and_compare(client, state, label="3oK(Ts)+Poly: 630 → integer")
    if r: results.append(r)

    return results


# =========================================================================
# FLINT TESTS — play against The Flint boss blind
# =========================================================================

def _is_boss_blind_select(state):
    """Check if we're at BLIND_SELECT with the boss blind on deck."""
    blinds = state.get("blinds", {})
    small_status = blinds.get("small", {}).get("status", "") if isinstance(blinds.get("small"), dict) else ""
    big_status = blinds.get("big", {}).get("status", "") if isinstance(blinds.get("big"), dict) else ""
    return small_status == "DEFEATED" and big_status == "DEFEATED"


def _beat_blind_fast(client, state):
    """Set chips to 999999 and play first 5 cards to instantly beat current blind."""
    try:
        client.call("set", {"chips": 999999})
    except APIError:
        pass
    hc = state.get("hand", {}).get("cards", [])
    if hc:
        client.call("play", {"cards": list(range(min(5, len(hc))))})
    time.sleep(0.5)


def _advance_to_boss(client, boss_blind_key, max_tries=50):
    """Advance the game until SELECTING_HAND at the boss blind.
    Forces boss_blind_key at BLIND_SELECT when the boss is on deck.
    Auto-beats small and big blinds along the way."""
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
                    print(f"  Forced boss blind={boss_blind_key} at BLIND_SELECT")
                except APIError as e:
                    print(f"  set blind failed: {e.message}")
            client.call("select")
            time.sleep(0.3)
        elif gs in ("HAND_PLAYED", "DRAW_TO_HAND", "NEW_ROUND"):
            time.sleep(0.3)
        else:
            time.sleep(0.3)
    raise TimeoutError(f"Never reached boss blind, stuck in {gs}")


def setup_flint_round(client, seed, joker_keys=None, card_configs=None):
    """Start a game, advance to The Flint boss round, and set up controlled
    jokers + hand. Forces bl_flint at BLIND_SELECT time (not at game start)."""
    try:
        client.call("menu")
    except APIError:
        pass
    time.sleep(0.5)
    client.call("start", {"deck": "RED", "stake": "WHITE", "seed": seed})
    state = wait_for_state(client, ["SELECTING_HAND"])

    # Set ante to 2 (Flint requires min ante 2)
    try:
        client.call("set", {"ante": 2})
        print(f"  Set ante=2")
    except APIError as e:
        print(f"  set ante failed: {e.message}")

    # Beat small blind fast
    _beat_blind_fast(client, state)

    # Advance through big blind → boss BLIND_SELECT, forcing bl_flint at boss selection
    state = _advance_to_boss(client, boss_blind_key="bl_flint")

    # Verify we're at The Flint
    blind_name, _ = get_current_blind(state)
    print(f"  At boss blind: {blind_name}")
    if "Flint" not in blind_name:
        print(f"  WARNING: Expected The Flint, got {blind_name}!")

    # Clear existing jokers
    for i in range(state.get("jokers", {}).get("count", 0)):
        try:
            client.call("sell", {"joker": 0})
        except APIError:
            pass

    # Discard existing hand
    for _ in range(3):
        state = client.call("gamestate")
        hc = state.get("hand", {}).get("cards", [])
        if hc:
            try:
                client.call("discard", {"cards": list(range(min(len(hc), 5)))})
                time.sleep(0.2)
            except APIError:
                break

    # Add jokers
    if joker_keys:
        for jk in joker_keys:
            params = {"key": jk} if isinstance(jk, str) else jk
            try:
                client.call("add", params)
            except APIError as e:
                print(f"  FAILED joker {params}: {e.message}")

    # Add controlled hand
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


def run_flint_tests(client):
    """Force The Flint as boss blind via set API, then run scoring tests.

    The Flint halves base chips and mult:
      chips → max(floor(chips * 0.5 + 0.5), 0)
      mult  → max(floor(mult  * 0.5 + 0.5), 1)

    The bot reads un-halved hand levels from the API, so every Flint hand
    should show a large negative mismatch (bot overestimates).
    """
    print("\n" + "=" * 60)
    print("FLINT TESTS — forcing The Flint boss blind")
    print("=" * 60)

    results = []

    pair_hand = [
        {"key": "S_K"}, {"key": "H_K"},
        {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"}
    ]
    tok_hand = [
        {"key": "S_K"}, {"key": "H_K"}, {"key": "D_K"},
        {"key": "C_4"}, {"key": "S_5"}
    ]
    fh_hand = [
        {"key": "S_K"}, {"key": "H_K"}, {"key": "D_K"},
        {"key": "S_A"}, {"key": "H_A"}
    ]
    hc_hand = [
        {"key": "S_K"}, {"key": "H_3"},
        {"key": "D_4"}, {"key": "C_6"}, {"key": "S_8"}
    ]

    # ---------------------------------------------------------------
    # Test 1: Flint + Pair (no jokers)
    # base 10/2 → halved 5/1. chips=5+10+10=25, mult=1. Total=25.
    # Bot (no halving): 30×2=60 → diff=-35
    # ---------------------------------------------------------------
    print(f"\n--- Flint Pair: base 10/2 → halved 5/1 ---")
    state = setup_flint_round(client, "FL1", card_configs=pair_hand)
    if state:
        hl = state.get("hands", {}).get("Pair", {})
        print(f"  API hand levels: chips={hl.get('chips','?')}, mult={hl.get('mult','?')}")
        r = play_and_compare(client, state, label="Flint Pair bare: 5/1 → 25")
        if r: results.append(r)

    # ---------------------------------------------------------------
    # Test 2: Flint + Three of a Kind (no jokers)
    # base 30/3 → halved 15/2. chips=15+10+10+10=45, mult=2. Total=90.
    # Bot: 60×3=180 → diff=-90
    # ---------------------------------------------------------------
    print(f"\n--- Flint 3oK: base 30/3 → halved 15/2 ---")
    state = setup_flint_round(client, "FL2", card_configs=tok_hand)
    if state:
        r = play_and_compare(client, state, label="Flint 3oK bare: 15/2 → 90")
        if r: results.append(r)

    # ---------------------------------------------------------------
    # Test 3: Flint + Full House (no jokers)
    # base 40/4 → halved 20/2. chips=20+10+10+10+11+11=72, mult=2.
    # Total=144. Bot: 92×4=368 → diff=-224
    # ---------------------------------------------------------------
    print(f"\n--- Flint Full House: base 40/4 → halved 20/2 ---")
    state = setup_flint_round(client, "FL3", card_configs=fh_hand)
    if state:
        r = play_and_compare(client, state, label="Flint FH bare: 20/2 → 144")
        if r: results.append(r)

    # ---------------------------------------------------------------
    # Test 4: Flint + High Card (no jokers)
    # base 5/1 → halved 3/1. chips=3+10=13, mult=1. Total=13.
    # Bot: 15×1=15 → diff=-2
    # ---------------------------------------------------------------
    print(f"\n--- Flint High Card: base 5/1 → halved 3/1 ---")
    state = setup_flint_round(client, "FL4", card_configs=hc_hand)
    if state:
        r = play_and_compare(client, state, label="Flint HC bare: 3/1 → 13")
        if r: results.append(r)

    # ---------------------------------------------------------------
    # Test 5: Flint + Cavendish + Pair
    # halved 5/1. Cavendish x3. 25×1×3=75.
    # Bot: 30×2×3=180 → diff=-105
    # ---------------------------------------------------------------
    print(f"\n--- Flint + Cavendish: Pair ---")
    state = setup_flint_round(client, "FL5", joker_keys=["j_cavendish"],
        card_configs=pair_hand)
    if state:
        dump_jokers(state.get("jokers", {}).get("cards", []))
        r = play_and_compare(client, state, label="Flint Pair + Cavendish: 75")
        if r: results.append(r)

    # ---------------------------------------------------------------
    # Test 6: Flint + Polychrome Joker + Pair
    # halved 5/1. Joker +4 → 5, Poly x1.5 → 7.5. 25×7.5=187.5, floor=187.
    # Bot: 30×(2+4)×1.5 = 270 → diff=-83
    # ---------------------------------------------------------------
    print(f"\n--- Flint + Poly Joker: Pair ---")
    state = setup_flint_round(client, "FL6",
        joker_keys=[{"key": "j_joker", "edition": "POLYCHROME"}],
        card_configs=pair_hand)
    if state:
        dump_jokers(state.get("jokers", {}).get("cards", []))
        r = play_and_compare(client, state, label="Flint Pair + Poly: 187")
        if r: results.append(r)

    # ---------------------------------------------------------------
    # Test 7: Flint + Polychrome Joker + Three of a Kind (3×7)
    # halved 15/2. Joker +4 → 6, Poly x1.5 → 9.0. chips=15+7+7+7=36.
    # 36×9=324. Bot: 51×10.5=535.5 → 536 → diff=-212
    # ---------------------------------------------------------------
    print(f"\n--- Flint + Poly Joker: 3oK(7s) ---")
    state = setup_flint_round(client, "FL7",
        joker_keys=[{"key": "j_joker", "edition": "POLYCHROME"}],
        card_configs=[
            {"key": "S_7"}, {"key": "H_7"}, {"key": "D_7"},
            {"key": "C_4"}, {"key": "S_5"}
        ])
    if state:
        r = play_and_compare(client, state, label="Flint 3oK(7s) + Poly: 324")
        if r: results.append(r)

    # ---------------------------------------------------------------
    # Test 8: Flint + Cavendish + Three of a Kind
    # halved 15/2. Cavendish x3. chips=45, mult=2×3=6. 45×6=270.
    # Bot: 60×3×3=540 → diff=-270
    # ---------------------------------------------------------------
    print(f"\n--- Flint + Cavendish: 3oK(Ks) ---")
    state = setup_flint_round(client, "FL8", joker_keys=["j_cavendish"],
        card_configs=tok_hand)
    if state:
        r = play_and_compare(client, state, label="Flint 3oK(Ks) + Cav: 270")
        if r: results.append(r)

    return results


# =========================================================================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=12346)
    parser.add_argument("--skip-flint", action="store_true", help="Skip Flint tests (slow)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    client = BalatroClient(port=args.port)
    try:
        client.call("health")
    except Exception as e:
        print(f"No server on port {args.port}: {e}")
        sys.exit(1)

    all_results = []

    # Rounding tests (fast — uses setup_game on small blind)
    all_results.extend(run_rounding_tests(client))

    # Flint tests (slow — must advance through 2 blinds per test)
    if not args.skip_flint:
        all_results.extend(run_flint_tests(client))
    else:
        print("\n  [Skipping Flint tests (--skip-flint)]")

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
        # Show whether floor would have matched
        floor_ok = " (floor=actual)" if r.get("est_floor") == r["actual"] else ""
        print(f"  {r['label']:50s} est={r['est']:>6d} actual={r['actual']:>6d} {status}{floor_ok}")

    print(f"\n  Total: {matches} match, {mismatches} mismatch out of {matches + mismatches}")


if __name__ == "__main__":
    main()
