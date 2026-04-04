"""Integration tests for batch 069 scoring fixes.

Reproduces the exact scenarios that exposed bugs 1-5 in-game and verifies
that our scoring estimate matches the game's actual score (diff == 0).

Fix 1: Four Fingers straight detection (sliding window)
Fix 2: Four Fingers Flush House / Flush Five scoring cards
Fix 3: Flower Pot + debuffed WILD cards (under The Head)
Fix 4: The Ox + Bull money zeroing
Fix 5: Luchador + The Mouth boss disable

Usage:
    python test_batch069_fixes.py --start-server
    python test_batch069_fixes.py --port 12346
"""

import argparse
import logging
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from balatrobot.cli.client import BalatroClient, APIError
from balatro_bot.domain.scoring.classify import classify_hand, _scoring_cards_for
from balatro_bot.domain.scoring.estimate import score_hand_detailed
from balatro_bot.cards import _modifier, card_rank, card_suits, is_joker_debuffed

from harness import (
    TEST_PORT, BOSS_MIN_ANTE,
    wait_for_state, setup_game, get_current_blind,
    advance_to_boss_select, advance_through_post_blind,
    force_boss, inject_jokers, set_ante, beat_blind_fast,
    ensure_server, stop_server, take_screenshot,
)
from scoring_diagnostics import fmt_card, dump_hand_detail


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def setup_clean(client, seed, joker_keys=None, card_configs=None):
    """Start fresh game, sell default jokers, discard hand, inject jokers + cards."""
    try:
        client.call("menu")
    except APIError:
        pass
    time.sleep(0.5)

    client.call("start", {"deck": "RED", "stake": "WHITE", "seed": seed})
    state = wait_for_state(client, {"SELECTING_HAND"})

    # Sell default jokers
    for _ in range(state.get("jokers", {}).get("count", 0)):
        try:
            client.call("sell", {"joker": 0})
        except APIError:
            pass

    # Discard hand twice to clear starting cards
    for _ in range(2):
        state = client.call("gamestate")
        hc = state.get("hand", {}).get("cards", [])
        if hc:
            try:
                client.call("discard", {"cards": list(range(min(len(hc), 5)))})
                time.sleep(0.2)
            except APIError:
                pass

    # Inject jokers
    if joker_keys:
        for jk in joker_keys:
            params = {"key": jk} if isinstance(jk, str) else jk
            try:
                client.call("add", params)
            except APIError as e:
                print(f"  FAILED joker {params}: {e.message}")

    # Inject playing cards
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


def play_and_compare(client, state, play_indices, label="", blind_name="",
                     ox_most_played=None):
    """Play cards at given indices, compare bot estimate vs actual."""
    hand_cards = state.get("hand", {}).get("cards", [])
    jokers = state.get("jokers", {}).get("cards", [])
    hand_levels = state.get("hands", {})

    played = [hand_cards[i] for i in play_indices if i < len(hand_cards)]
    held = [c for j, c in enumerate(hand_cards) if j not in set(play_indices)]

    joker_keys_set = {j.get("key") for j in jokers if not is_joker_debuffed(j)}
    four_fingers = "j_four_fingers" in joker_keys_set
    shortcut = "j_shortcut" in joker_keys_set
    smeared = "j_smeared" in joker_keys_set

    hand_name = classify_hand(
        played,
        four_fingers=four_fingers,
        shortcut=shortcut,
        smeared=smeared,
    )
    scoring = played if "j_splash" in joker_keys_set else _scoring_cards_for(
        hand_name, played, four_fingers=four_fingers, smeared=smeared,
        shortcut=shortcut)

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
        blind_name=blind_name,
        deck_count=state.get("cards", {}).get("count", 0),
        deck_cards=state.get("cards", {}).get("cards", []),
        ox_most_played=ox_most_played,
    )

    pre_chips = state.get("round", {}).get("chips", 0)

    played_str = ", ".join(fmt_card(c) for c in played)
    scoring_str = ", ".join(fmt_card(c) for c in scoring)
    print(f"\n  [{label}]")
    print(f"    Hand:    {hand_name}")
    print(f"    Played:  [{played_str}] ({len(played)} cards)")
    print(f"    Scoring: [{scoring_str}] ({len(scoring)} cards)")
    print(f"    Base: {detail['base_chips']}/{detail['base_mult']}")
    for entry in detail.get("joker_contributions", []):
        jlabel, dc, dm = entry[0], entry[1], entry[2]
        xm = entry[3] if len(entry) > 3 else 1.0
        parts = []
        if dc:
            parts.append(f"+{dc:.0f}c")
        if xm > 1.01 or xm < 0.99:
            parts.append(f"x{xm:.2f}")
        elif dm:
            parts.append(f"+{dm:.1f}m")
        if parts:
            print(f"      {jlabel}: {', '.join(parts)}")
    print(f"    Estimate: {detail['total']}")

    try:
        new_state = client.call("play", {"cards": play_indices})
    except APIError as e:
        print(f"    Play failed: {e.message}")
        return {"label": label, "est": detail["total"], "actual": 0, "diff": 0,
                "hand_name": hand_name, "error": True}

    post_chips = new_state.get("round", {}).get("chips", 0)
    actual = post_chips - pre_chips
    diff = actual - detail["total"]

    status = "MATCH" if diff == 0 else f"MISMATCH({diff:+d})"
    print(f"    Actual:   {actual}")
    print(f"    >> {status}")

    if diff != 0:
        try:
            take_screenshot(client, f"batch069_{label}")
        except Exception:
            pass  # screenshot is best-effort, don't crash the test

    return {"label": label, "est": detail["total"], "actual": actual, "diff": diff,
            "hand_name": hand_name, "new_state": new_state}


# ---------------------------------------------------------------------------
# Fix 1+2: Four Fingers — comprehensive edge case diagnostics
#
# Tests every combination to determine exactly which cards score in-game:
#   ff_a: 4-card straight + off-rank (4567+A, all diff suits)
#   ff_b: 4-card straight + duplicate rank within range (67789, all diff suits)
#   ff_c: 4-card flush + off-suit (all hearts + 1 spade, no straight)
#   ff_d: Flush House — 4-card flush + full house (KKK99, 4 hearts + 1 diamond)
#   ff_e: SF case A — 4 suited + 4 consecutive overlap (4♥5♥6♥7♥ + A♠)
#   ff_f: SF case B — 5 suited + 4 consecutive (4♥5♥6♥7♥ + K♥)
#   ff_g: SF case C — 4 suited + 5 consecutive, flush/straight subsets differ
#                      (4♥ 5♥ 6♠ 7♥ 8♥)
# ---------------------------------------------------------------------------

def _run_ff_case(client, results, seed, label, card_configs, expected_hand=None):
    """Helper: set up Four Fingers + inject cards, play last 5, compare."""
    print(f"\n{'='*60}")
    print(f"FF: {label}")
    print(f"{'='*60}")

    state = setup_clean(client, seed,
        joker_keys=["j_four_fingers"],
        card_configs=card_configs)

    hand_cards = state.get("hand", {}).get("cards", [])
    print(f"  Hand: {len(hand_cards)} cards")
    for i, c in enumerate(hand_cards):
        print(f"    [{i}] {fmt_card(c)}")

    play_indices = list(range(len(hand_cards) - 5, len(hand_cards)))
    print(f"  Playing indices: {play_indices}")
    r = play_and_compare(client, state, play_indices, label)

    if expected_hand and r["hand_name"] != expected_hand:
        print(f"    !! CLASSIFICATION: expected {expected_hand}, got {r['hand_name']}")
    results.append(r)


def test_ff_straight_offrank(client, results):
    """FF case A: 4-card straight [4,5,6,7] + off-rank Ace (all diff suits)."""
    _run_ff_case(client, results, "B069_FFA",
        "Straight: 4567 + A (off-rank, all diff suits)",
        [{"key": "H_4"}, {"key": "D_5"}, {"key": "C_6"},
         {"key": "S_7"}, {"key": "H_A"}],
        expected_hand="Straight")


def test_ff_straight_duprank(client, results):
    """FF case B: 4-card straight with duplicate rank [6,7,7,8,9]."""
    _run_ff_case(client, results, "B069_FFB",
        "Straight: 67789 (dup rank in range, all diff suits)",
        [{"key": "H_6"}, {"key": "D_7"}, {"key": "C_7"},
         {"key": "S_8"}, {"key": "H_9"}],
        expected_hand="Straight")


def test_ff_flush_offsuit(client, results):
    """FF case C: 4-card flush (4 hearts) + 1 off-suit, no straight."""
    _run_ff_case(client, results, "B069_FFC",
        "Flush: 4 hearts + 1 spade (no straight)",
        [{"key": "H_2"}, {"key": "H_5"}, {"key": "H_8"},
         {"key": "H_J"}, {"key": "S_A"}],
        expected_hand="Flush")


def test_ff_flush_house(client, results):
    """FF case D: Flush House — KKK99, 4 hearts + 1 diamond."""
    _run_ff_case(client, results, "B069_FFD",
        "Flush House: KKK99 (4 hearts + 1 diamond 9)",
        [{"key": "H_K"}, {"key": "H_K"}, {"key": "H_K"},
         {"key": "H_9"}, {"key": "D_9"}],
        expected_hand="Flush House")


def test_ff_sf_pure(client, results):
    """FF case E: SF — 4 suited consecutive (4♥5♥6♥7♥) + off-suit off-rank (A♠)."""
    _run_ff_case(client, results, "B069_FFE",
        "SF: 4H 5H 6H 7H + AS (off-suit, off-rank)",
        [{"key": "H_4"}, {"key": "H_5"}, {"key": "H_6"},
         {"key": "H_7"}, {"key": "S_A"}],
        expected_hand="Straight Flush")


def test_ff_sf_5flush_4straight(client, results):
    """FF case F: SF — 5 suited, 4 consecutive (4♥5♥6♥7♥K♥). K is flush-only."""
    _run_ff_case(client, results, "B069_FFF",
        "SF: 4H 5H 6H 7H KH (5-flush, 4-straight, K flush-only)",
        [{"key": "H_4"}, {"key": "H_5"}, {"key": "H_6"},
         {"key": "H_7"}, {"key": "H_K"}],
        expected_hand="Straight Flush")


def test_ff_sf_disjoint(client, results):
    """FF case G: SF — flush/straight subsets differ (4♥ 5♥ 6♠ 7♥ 8♥).
    Flush = {4♥,5♥,7♥,8♥}, Straight = {4,5,6,7} or {5,6,7,8}. 6♠ off-suit."""
    _run_ff_case(client, results, "B069_FFG",
        "SF: 4H 5H 6S 7H 8H (disjoint flush/straight subsets)",
        [{"key": "H_4"}, {"key": "H_5"}, {"key": "S_6"},
         {"key": "H_7"}, {"key": "H_8"}],
        expected_hand="Straight Flush")


# ---------------------------------------------------------------------------
# Fix 3: Flower Pot + debuffed WILD under The Head
# ---------------------------------------------------------------------------

def test_flower_pot_debuffed_wild(client, results):
    """Flower Pot: debuffed WILD (Heart under The Head) should NOT fill suit."""
    print(f"\n{'='*60}")
    print("FIX 3: Flower Pot — debuffed WILD under The Head")
    print(f"{'='*60}")

    # Setup: force The Head (debuffs Hearts), inject Flower Pot
    state = setup_clean(client, "B069_FP1",
        joker_keys=["j_flower_pot"],
        card_configs=[
            # 3 natural non-Heart suits + 1 Heart WILD (will be debuffed)
            {"key": "S_K"},                          # Spade (natural)
            {"key": "D_Q"},                          # Diamond (natural)
            {"key": "C_J"},                          # Club (natural)
            {"key": "H_T", "enhancement": "WILD"},   # Heart WILD — debuffed by The Head
            {"key": "S_9"},                          # Spade (pad)
        ])

    # Need to advance to The Head boss blind
    # Beat SB quickly
    state = client.call("gamestate")
    beat_blind_fast(client, state)
    state = advance_through_post_blind(client)

    # Beat BB quickly
    if state.get("state") == "BLIND_SELECT":
        client.call("select")
        time.sleep(0.3)
    state = wait_for_state(client, {"SELECTING_HAND"})
    beat_blind_fast(client, state)
    state = advance_through_post_blind(client)

    # Force The Head as boss
    if state.get("state") == "BLIND_SELECT":
        if not force_boss(client, "The Head"):
            print("  SKIP: Could not force The Head")
            return

    client.call("select")
    time.sleep(0.3)
    state = wait_for_state(client, {"SELECTING_HAND"})

    # Re-inject our cards (hand was refilled)
    # Sell existing jokers, re-add flower pot + our cards
    for _ in range(state.get("jokers", {}).get("count", 0)):
        try:
            client.call("sell", {"joker": 0})
        except APIError:
            pass
    # Discard to clear
    for _ in range(2):
        state = client.call("gamestate")
        hc = state.get("hand", {}).get("cards", [])
        if hc:
            try:
                client.call("discard", {"cards": list(range(min(len(hc), 5)))})
                time.sleep(0.2)
            except APIError:
                pass

    try:
        client.call("add", {"key": "j_flower_pot"})
    except APIError as e:
        print(f"  FAILED adding flower_pot: {e.message}")
    for cfg in [
        {"key": "S_K"},
        {"key": "D_Q"},
        {"key": "C_J"},
        {"key": "H_T", "enhancement": "WILD"},   # Heart WILD — debuffed by The Head
        {"key": "S_9"},
    ]:
        params = {"key": cfg["key"]}
        if "enhancement" in cfg:
            params["enhancement"] = cfg["enhancement"]
        try:
            client.call("add", params)
        except APIError as e:
            print(f"  FAILED card: {e.message}")

    time.sleep(0.3)
    state = client.call("gamestate")

    blind_name, _ = get_current_blind(state)
    print(f"  Blind: {blind_name}")

    # Verify the Heart WILD is debuffed
    hand_cards = state.get("hand", {}).get("cards", [])
    print(f"  Hand: {len(hand_cards)} cards")
    for i, c in enumerate(hand_cards):
        debuf = (c.get("state") or {}).get("debuff", False)
        enh = _modifier(c).get("enhancement", "-")
        rank = c.get("value", {}).get("rank", "?")
        suit = c.get("value", {}).get("suit", "?")
        if debuf or enh == "WILD":
            print(f"    [{i}] {rank}{suit} enh={enh} debuff={debuf}")

    play_indices = list(range(len(hand_cards) - 5, len(hand_cards)))
    r = play_and_compare(client, state, play_indices,
                         "FlowerPot: debuffed WILD (Heart under The Head)",
                         blind_name=blind_name)
    results.append(r)


def test_flower_pot_non_debuffed_wild(client, results):
    """Control: Flower Pot with non-debuffed WILD should trigger (no boss debuff)."""
    print(f"\n{'='*60}")
    print("FIX 3 CONTROL: Flower Pot — non-debuffed WILD (no boss)")
    print(f"{'='*60}")

    # On Small Blind (no boss debuff), WILD should fill the missing suit
    state = setup_clean(client, "B069_FP2",
        joker_keys=["j_flower_pot"],
        card_configs=[
            {"key": "S_K"},
            {"key": "D_Q"},
            {"key": "C_J"},
            {"key": "S_T", "enhancement": "WILD"},   # WILD Spade — fills Heart
            {"key": "S_9"},
        ])

    hand_cards = state.get("hand", {}).get("cards", [])
    print(f"  Hand: {len(hand_cards)} cards")
    play_indices = list(range(len(hand_cards) - 5, len(hand_cards)))
    r = play_and_compare(client, state, play_indices,
                         "FlowerPot: non-debuffed WILD (control)")
    results.append(r)


# ---------------------------------------------------------------------------
# Fix 4: The Ox + Bull money zeroing
# ---------------------------------------------------------------------------

def test_ox_bull(client, results):
    """The Ox zeroes money when playing most-played hand → Bull gets $0."""
    from balatro_bot.domain.scoring.estimate import ox_most_played_hand

    print(f"\n{'='*60}")
    print("FIX 4: The Ox + Bull — money zeroed on most-played hand")
    print(f"{'='*60}")

    # Start fresh — we need to build up a clear most-played hand type
    # by playing several Pair hands before reaching The Ox.
    state = setup_clean(client, "B069_OX1", joker_keys=["j_bull"])

    # Play a few Pair hands on small blind to establish Pair as most-played.
    # Inject pairs to guarantee it.
    for i in range(3):
        state = client.call("gamestate")
        if state.get("state") != "SELECTING_HAND":
            break
        # Discard current hand
        hc = state.get("hand", {}).get("cards", [])
        if hc:
            try:
                client.call("discard", {"cards": list(range(min(len(hc), 5)))})
                time.sleep(0.2)
            except APIError:
                pass
        # Inject a pair
        for cfg in [{"key": "H_K"}, {"key": "D_K"}, {"key": "C_3"},
                    {"key": "S_5"}, {"key": "H_7"}]:
            try:
                client.call("add", {"key": cfg["key"]})
            except APIError:
                pass
        time.sleep(0.2)
        state = client.call("gamestate")
        hc = state.get("hand", {}).get("cards", [])
        play_indices = list(range(len(hc) - 5, len(hc)))
        try:
            state = client.call("play", {"cards": play_indices})
        except APIError:
            pass
        time.sleep(0.3)
        # Wait to return to SELECTING_HAND or advance
        for _ in range(20):
            state = client.call("gamestate")
            if state.get("state") in ("SELECTING_HAND", "BLIND_SELECT",
                                       "SHOP", "GAME_OVER"):
                break
            time.sleep(0.3)

    # Now advance to ante 6 boss select
    state = advance_to_boss_select(client, target_ante=6)

    if not force_boss(client, "The Ox"):
        print("  SKIP: Could not force The Ox")
        return

    client.call("select")
    time.sleep(0.3)
    state = wait_for_state(client, {"SELECTING_HAND"})

    # Snapshot the most-played hand at blind start (game locks this)
    hand_levels = state.get("hands", {})
    ox_locked = ox_most_played_hand(hand_levels)
    print(f"  Ox locked most-played: '{ox_locked}'")

    # Show all played counts
    for ht, data in hand_levels.items():
        if isinstance(data, dict) and data.get("played", 0) > 0:
            print(f"    {ht}: played={data['played']}")

    # Re-inject Bull
    for _ in range(state.get("jokers", {}).get("count", 0)):
        try:
            client.call("sell", {"joker": 0})
        except APIError:
            pass
    try:
        client.call("add", {"key": "j_bull"})
    except APIError as e:
        print(f"  FAILED adding bull: {e.message}")

    # Set known money
    try:
        client.call("set", {"money": 200})
    except APIError:
        pass

    blind_name, _ = get_current_blind(state)
    print(f"  Blind: {blind_name}")

    # Discard and inject a pair (to play the locked most-played hand type)
    state = client.call("gamestate")
    hc = state.get("hand", {}).get("cards", [])
    if hc:
        try:
            client.call("discard", {"cards": list(range(min(len(hc), 5)))})
            time.sleep(0.2)
        except APIError:
            pass
    for cfg in [{"key": "H_K"}, {"key": "D_K"}, {"key": "C_3"},
                {"key": "S_5"}, {"key": "H_7"}]:
        try:
            client.call("add", {"key": cfg["key"]})
        except APIError:
            pass

    time.sleep(0.3)
    state = client.call("gamestate")
    hand_cards = state.get("hand", {}).get("cards", [])
    play_indices = list(range(len(hand_cards) - 5, len(hand_cards)))
    print(f"  Money before play: {state.get('money', '?')}")

    r = play_and_compare(client, state, play_indices,
                         f"Ox+Bull: playing Pair (locked={ox_locked}), money={state.get('money', '?')}",
                         blind_name=blind_name,
                         ox_most_played=ox_locked)
    results.append(r)

    # Second hand: play a High Card (NOT the locked type) — money should be kept
    time.sleep(0.3)
    for _ in range(20):
        state = client.call("gamestate")
        if state.get("state") == "SELECTING_HAND":
            break
        time.sleep(0.3)

    if state.get("state") == "SELECTING_HAND":
        # Discard and inject non-pair cards
        hc = state.get("hand", {}).get("cards", [])
        if hc:
            try:
                client.call("discard", {"cards": list(range(min(len(hc), 5)))})
                time.sleep(0.2)
            except APIError:
                pass
        for cfg in [{"key": "H_2"}, {"key": "D_5"}, {"key": "C_8"},
                    {"key": "S_J"}, {"key": "H_A"}]:
            try:
                client.call("add", {"key": cfg["key"]})
            except APIError:
                pass
        # Set money again (The Ox may have zeroed it)
        try:
            client.call("set", {"money": 200})
        except APIError:
            pass

        time.sleep(0.3)
        state = client.call("gamestate")
        hand_cards = state.get("hand", {}).get("cards", [])
        play_indices = list(range(len(hand_cards) - 5, len(hand_cards)))
        print(f"  Money before h2: {state.get('money', '?')}")

        r2 = play_and_compare(client, state, play_indices,
                              f"Ox+Bull h2: High Card (locked={ox_locked}), money={state.get('money', '?')}",
                              blind_name=blind_name,
                              ox_most_played=ox_locked)
        results.append(r2)


# ---------------------------------------------------------------------------
# Fix 5: Luchador + The Mouth boss disable
# ---------------------------------------------------------------------------

def test_luchador_mouth(client, results):
    """Sell Luchador to disable The Mouth, then play a different hand type."""
    print(f"\n{'='*60}")
    print("FIX 5: Luchador + The Mouth — boss disable after sell")
    print(f"{'='*60}")

    # Start fresh, advance to ante 2 (The Mouth min ante)
    state = setup_clean(client, "B069_LM1")

    state = advance_to_boss_select(client, target_ante=2)

    if not force_boss(client, "The Mouth"):
        print("  SKIP: Could not force The Mouth")
        return

    client.call("select")
    time.sleep(0.3)
    state = wait_for_state(client, {"SELECTING_HAND"})

    # Re-inject Luchador
    for _ in range(state.get("jokers", {}).get("count", 0)):
        try:
            client.call("sell", {"joker": 0})
        except APIError:
            pass
    try:
        client.call("add", {"key": "j_luchador"})
    except APIError as e:
        print(f"  FAILED adding luchador: {e.message}")

    blind_name, _ = get_current_blind(state)
    print(f"  Blind: {blind_name}")

    # Step 1: Play a hand to lock The Mouth to that hand type
    state = client.call("gamestate")
    hand_cards = state.get("hand", {}).get("cards", [])
    n = min(5, len(hand_cards))
    play_indices = list(range(n))

    played = [hand_cards[i] for i in play_indices]
    lock_hand = classify_hand(played)
    print(f"\n  Step 1: Playing {lock_hand} to lock The Mouth")

    try:
        state = client.call("play", {"cards": play_indices})
    except APIError as e:
        print(f"  Play failed: {e.message}")
        return

    # Wait to get back to SELECTING_HAND
    time.sleep(0.3)
    for _ in range(20):
        state = client.call("gamestate")
        if state.get("state") == "SELECTING_HAND":
            break
        time.sleep(0.3)

    if state.get("state") != "SELECTING_HAND":
        print(f"  Stuck in {state.get('state')} — may have won the blind already")
        return

    # Verify The Mouth is locked
    hand_levels = state.get("hands", {})
    locked = None
    for ht, data in hand_levels.items():
        if isinstance(data, dict) and data.get("played_this_round", 0) > 0:
            locked = ht
            break
    print(f"  Mouth locked to: {locked}")

    # Step 2: Sell Luchador to disable The Mouth
    print(f"\n  Step 2: Selling Luchador to disable The Mouth")
    luchador_idx = None
    jokers = state.get("jokers", {}).get("cards", [])
    for i, j in enumerate(jokers):
        if j.get("key") == "j_luchador":
            luchador_idx = i
            break

    if luchador_idx is None:
        print("  ERROR: Luchador not found in jokers!")
        return

    try:
        state = client.call("sell", {"joker": luchador_idx})
    except APIError as e:
        print(f"  Sell failed: {e.message}")
        return

    time.sleep(0.3)
    state = client.call("gamestate")
    print(f"  State after sell: {state.get('state')}")

    if state.get("state") != "SELECTING_HAND":
        print(f"  Stuck in {state.get('state')} after sell")
        return

    # Step 3: Play a hand — with The Mouth disabled, ANY hand type should score
    hand_cards = state.get("hand", {}).get("cards", [])
    n = min(5, len(hand_cards))
    play_indices = list(range(n))

    played = [hand_cards[i] for i in play_indices]
    new_hand = classify_hand(played)
    print(f"\n  Step 3: Playing {new_hand} after Luchador sell")
    if locked and new_hand == locked:
        print(f"    (Same as locked type — boss disable less observable but score should still match)")

    # The key test: our estimate should match actual.
    # Before the fix, if new_hand != locked, the bot would estimate 0 (Mouth blocks it)
    # but the game would score normally (Mouth is disabled).
    r = play_and_compare(client, state, play_indices,
                         f"Luchador sold: playing {new_hand} (was locked to {locked})",
                         blind_name="")  # Empty blind_name = boss disabled
    results.append(r)

    # Verify score is non-zero (boss should be disabled)
    if r["actual"] == 0 and new_hand != locked:
        print(f"    !! Score is 0 — The Mouth may still be active despite Luchador sell!")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Integration tests for batch 069 scoring fixes")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--seed", type=str, default="B069")
    parser.add_argument("--start-server", action="store_true")
    parser.add_argument("--test", type=str, default="all",
                        help="Run specific test: ff_a-ff_g, fp, fp_ctrl, ox, luchador, or 'all'")
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

    results = []
    tests = {
        "ff_a": ("FF: Straight 4567+A (off-rank)", test_ff_straight_offrank),
        "ff_b": ("FF: Straight 67789 (dup rank)", test_ff_straight_duprank),
        "ff_c": ("FF: Flush 4-hearts + off-suit", test_ff_flush_offsuit),
        "ff_d": ("FF: Flush House KKK99", test_ff_flush_house),
        "ff_e": ("FF: SF pure 4H5H6H7H+AS", test_ff_sf_pure),
        "ff_f": ("FF: SF 5-flush 4-straight", test_ff_sf_5flush_4straight),
        "ff_g": ("FF: SF disjoint subsets", test_ff_sf_disjoint),
        "fp": ("Fix 3: Flower Pot debuffed WILD", test_flower_pot_debuffed_wild),
        "fp_ctrl": ("Fix 3 Control: Flower Pot non-debuffed WILD", test_flower_pot_non_debuffed_wild),
        "ox": ("Fix 4: The Ox + Bull", test_ox_bull),
        "luchador": ("Fix 5: Luchador + The Mouth", test_luchador_mouth),
    }

    if args.test == "all":
        run_tests = list(tests.keys())
    else:
        run_tests = [t.strip() for t in args.test.split(",")]
        for t in run_tests:
            if t not in tests:
                print(f"Unknown test '{t}'. Available: {', '.join(tests.keys())}")
                sys.exit(1)

    for tid in run_tests:
        label, func = tests[tid]
        print(f"\n\n{'#'*70}")
        print(f"# {label}")
        print(f"{'#'*70}")
        try:
            func(client, results)
        except Exception as e:
            print(f"\n  !! TEST FAILED WITH EXCEPTION: {e}")
            import traceback
            traceback.print_exc()
            results.append({"label": label, "est": 0, "actual": 0, "diff": -1,
                            "hand_name": "ERROR", "error": True})

    # --- Summary ---
    print(f"\n\n{'#'*70}")
    print(f"# SUMMARY")
    print(f"{'#'*70}")

    matches = 0
    mismatches = 0
    errors = 0
    for r in results:
        if r.get("error"):
            errors += 1
            status = "ERROR"
        elif r["diff"] == 0:
            matches += 1
            status = "MATCH"
        else:
            mismatches += 1
            status = f"MISMATCH({r['diff']:+d})"
        print(f"  {status:>16s}  {r['hand_name']:20s}  est={r['est']:>8d}  actual={r['actual']:>8d}  | {r['label']}")

    print(f"\n  Total: {len(results)} tests | {matches} MATCH | {mismatches} MISMATCH | {errors} ERROR")

    if mismatches > 0:
        print(f"\n  !! {mismatches} MISMATCHES DETECTED — fixes may not be working correctly")
    elif errors > 0:
        print(f"\n  !! {errors} ERRORS — some tests could not complete")
    else:
        print(f"\n  ALL TESTS MATCH — batch 069 fixes verified in-game")

    if server_proc:
        stop_server(server_proc)


if __name__ == "__main__":
    main()
