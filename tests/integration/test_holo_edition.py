"""Test HOLO edition mult scoring — diagnose contamination from enhancements.

Injects cards with HOLO edition + various enhancements (MULT, LUCKY, none)
and compares bot scoring estimate vs actual game score. Also dumps the raw
API fields to detect whether card.edition.mult is contaminated by the
enhancement's ability.mult value.

Check the lovely console for BB.EDITION debug messages from gamestate.lua.

Usage:
    python test_holo_edition.py [--port PORT]

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
from balatro_bot.domain.scoring.estimate import score_hand_detailed
from balatro_bot.cards import card_chip_value, card_mult_value, card_xmult_value, _modifier


def wait_for_state(client, target_states, max_tries=30):
    """Poll until game reaches one of the target states."""
    for _ in range(max_tries):
        state = client.call("gamestate")
        gs = state.get("state", "")
        if gs in target_states:
            return state
        if gs == "BLIND_SELECT":
            client.call("select")
            time.sleep(0.3)
            continue
        time.sleep(0.3)
    raise TimeoutError(f"Never reached {target_states}, stuck in {state.get('state')}")


def dump_card(i, c):
    """Print detailed card info."""
    mod = _modifier(c)
    rank = c.get("value", {}).get("rank", "?")
    suit = c.get("value", {}).get("suit", "?")
    enh = mod.get("enhancement", "-")
    ed = mod.get("edition", "-")
    ed_mult = mod.get("edition_mult")
    ed_xmult = mod.get("edition_x_mult")
    enh_xmult = mod.get("enhancement_x_mult")
    perma = c.get("value", {}).get("perma_bonus", 0)

    chips = card_chip_value(c)
    mult = card_mult_value(c)
    xmult = card_xmult_value(c)

    print(f"  [{i}] {rank}{suit} enh={enh} ed={ed} | "
          f"edition_mult={ed_mult} edition_x_mult={ed_xmult} enh_x_mult={enh_xmult} | "
          f"chips={chips} mult={mult} xmult={xmult} perma={perma} | "
          f"raw_mod={mod}")


def run_test(client, label, card_configs, seed):
    """Run one scoring test: inject cards, play them, compare est vs actual."""
    print(f"\n{'='*60}")
    print(f"TEST: {label}")
    print(f"{'='*60}")

    # Start a fresh game
    try:
        client.call("menu")
    except APIError:
        pass
    time.sleep(0.5)

    state = client.call("start", {"deck": "RED", "stake": "WHITE", "seed": seed})
    print(f"Game started: seed={state.get('seed')}")

    # Get to SELECTING_HAND
    state = wait_for_state(client, ["SELECTING_HAND"])

    # Sell all jokers
    for i in range(state.get("jokers", {}).get("count", 0)):
        try:
            client.call("sell", {"joker": 0})
        except APIError:
            pass

    # Discard entire hand
    hand_cards = state.get("hand", {}).get("cards", [])
    if hand_cards:
        try:
            client.call("discard", {"cards": list(range(min(len(hand_cards), 5)))})
            time.sleep(0.2)
        except APIError:
            pass

    # Discard again to clear remaining cards
    state = client.call("gamestate")
    hand_cards = state.get("hand", {}).get("cards", [])
    if hand_cards:
        try:
            client.call("discard", {"cards": list(range(min(len(hand_cards), 5)))})
            time.sleep(0.2)
        except APIError:
            pass

    # Inject test cards
    print(f"\nInjecting {len(card_configs)} cards...")
    for cfg in card_configs:
        params = {"key": cfg["key"]}
        if "edition" in cfg:
            params["edition"] = cfg["edition"]
        if "enhancement" in cfg:
            params["enhancement"] = cfg["enhancement"]
        try:
            client.call("add", params)
            print(f"  Added {cfg['key']} (ed={cfg.get('edition','-')} enh={cfg.get('enhancement','-')})")
        except APIError as e:
            print(f"  FAILED: {cfg['key']}: {e.message}")

    # Re-read state
    time.sleep(0.3)
    state = client.call("gamestate")
    hand_cards = state.get("hand", {}).get("cards", [])
    print(f"\nHand has {len(hand_cards)} cards:")
    for i, c in enumerate(hand_cards):
        dump_card(i, c)

    # Find the injected cards (they should be the last N)
    n = len(card_configs)
    play_indices = list(range(max(0, len(hand_cards) - n), len(hand_cards)))
    played = [hand_cards[i] for i in play_indices]

    hand_name = classify_hand(played)
    jokers = state.get("jokers", {}).get("cards", [])
    hand_levels = state.get("hands", {})
    held = [c for j, c in enumerate(hand_cards) if j not in set(play_indices)]

    scoring = _scoring_cards_for(hand_name, played)
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
    )

    pre_chips = state.get("round", {}).get("chips", 0)
    print(f"\nPlaying {hand_name}: indices={play_indices}")
    print(f"  Base: {detail['base_chips']}/{detail['base_mult']}")
    print(f"  Pre-joker: {detail['pre_joker_chips']}/{detail['pre_joker_mult']:.1f}")
    print(f"  Post-joker: {detail['post_joker_chips']}/{detail['post_joker_mult']:.1f}")
    print(f"  Bot estimate: {detail['total']}")

    # Play
    try:
        new_state = client.call("play", {"cards": play_indices})
    except APIError as e:
        print(f"  Play failed: {e.message}")
        return None

    post_chips = new_state.get("round", {}).get("chips", 0)
    actual = post_chips - pre_chips
    diff = actual - detail["total"]

    print(f"  Actual score: {actual}")
    print(f"  Difference: {diff:+d}")

    if actual > 0 and diff == 0:
        print("  >> MATCH")
    elif actual > 0:
        ratio = detail["total"] / actual if actual else 0
        print(f"  >> MISMATCH (est/actual = {ratio:.3f})")
    else:
        print("  >> INCONCLUSIVE (actual=0)")

    return {"label": label, "est": detail["total"], "actual": actual, "diff": diff}


def main():
    parser = argparse.ArgumentParser(description="Test HOLO edition mult scoring")
    parser.add_argument("--port", type=int, default=12346)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    client = BalatroClient(port=args.port)
    try:
        client.call("health")
    except Exception as e:
        print(f"No server on port {args.port}: {e}")
        sys.exit(1)

    results = []

    # Test 1: HOLO only (no enhancement) — clean baseline
    results.append(run_test(client, "HOLO only (no enhancement)", [
        {"key": "S_A", "edition": "HOLO"},
        {"key": "H_A", "edition": "HOLO"},
        {"key": "D_K"},
        {"key": "C_K"},
        {"key": "S_Q"},
    ], seed="HOLO1"))

    # Test 2: HOLO + MULT enhancement — the suspected contamination case
    results.append(run_test(client, "HOLO + MULT enhancement", [
        {"key": "S_A", "edition": "HOLO", "enhancement": "MULT"},
        {"key": "H_A", "edition": "HOLO", "enhancement": "MULT"},
        {"key": "D_K"},
        {"key": "C_K"},
        {"key": "S_Q"},
    ], seed="HOLO2"))

    # Test 3: HOLO + LUCKY enhancement
    results.append(run_test(client, "HOLO + LUCKY enhancement", [
        {"key": "S_A", "edition": "HOLO", "enhancement": "LUCKY"},
        {"key": "H_A", "edition": "HOLO", "enhancement": "LUCKY"},
        {"key": "D_K"},
        {"key": "C_K"},
        {"key": "S_Q"},
    ], seed="HOLO3"))

    # Test 4: HOLO + GLASS enhancement (xmult interaction)
    results.append(run_test(client, "HOLO + GLASS enhancement", [
        {"key": "S_A", "edition": "HOLO", "enhancement": "GLASS"},
        {"key": "H_A", "edition": "HOLO", "enhancement": "GLASS"},
        {"key": "D_K"},
        {"key": "C_K"},
        {"key": "S_Q"},
    ], seed="HOLO4"))

    # Test 5: FOIL only — verify chip edition works
    results.append(run_test(client, "FOIL only (chip baseline)", [
        {"key": "S_A", "edition": "FOIL"},
        {"key": "H_A", "edition": "FOIL"},
        {"key": "D_K"},
        {"key": "C_K"},
        {"key": "S_Q"},
    ], seed="FOIL1"))

    # Test 6: HOLO + BONUS enhancement (no mult interaction)
    results.append(run_test(client, "HOLO + BONUS enhancement", [
        {"key": "S_A", "edition": "HOLO", "enhancement": "BONUS"},
        {"key": "H_A", "edition": "HOLO", "enhancement": "BONUS"},
        {"key": "D_K"},
        {"key": "C_K"},
        {"key": "S_Q"},
    ], seed="HOLO5"))

    # Summary
    print(f"\n\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    for r in results:
        if r is None:
            continue
        status = "MATCH" if r["diff"] == 0 else f"MISMATCH({r['diff']:+d})"
        print(f"  {r['label']:40s} est={r['est']:>8d} actual={r['actual']:>8d} {status}")

    print(f"\nCheck the lovely console for BB.EDITION debug messages!")
    print("Look for: raw_edition={{...}} to see if card.edition.mult is contaminated")


if __name__ == "__main__":
    main()
