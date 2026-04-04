"""Test MULT enhancement + HOLO edition interaction.

Dump raw modifier data and score a hand with:
1. MULT-only card (no edition)
2. HOLO-only card (no enhancement)
3. MULT + HOLO card (both)
4. Plain card (baseline)

Usage:
    python test_mult_holo.py [--port PORT]
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
from balatro_bot.cards import card_chip_value, card_mult_value, card_edition_mult_value, card_xmult_value, card_edition_xmult_value, _modifier


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
        time.sleep(0.3)
    raise TimeoutError(f"Never reached {target_states}, stuck in {state.get('state')}")


def setup_game(client, seed, joker_keys=None, card_configs=None):
    try:
        client.call("menu")
    except APIError:
        pass
    time.sleep(0.5)
    state = client.call("start", {"deck": "RED", "stake": "WHITE", "seed": seed})
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


def dump_card(card):
    """Dump raw card data relevant to scoring."""
    label = card.get("label", "?")
    mod = card.get("modifier", {})
    val = card.get("value", {})
    rank = val.get("rank", "?")
    suit = val.get("suit", "?")
    print(f"    {label} ({rank}{suit})")
    print(f"      raw modifier = {json.dumps(mod, indent=None)}")
    m = _modifier(card)
    print(f"      enhancement  = {m.get('enhancement', '(none)')}")
    print(f"      edition      = {m.get('edition', '(none)')}")
    print(f"      edition_mult = {m.get('edition_mult', '(none)')}")
    print(f"      edition_chips= {m.get('edition_chips', '(none)')}")
    print(f"      edition_x_mult= {m.get('edition_x_mult', '(none)')}")
    print(f"      bot chip_val = {card_chip_value(card)}")
    print(f"      bot mult_val = {card_mult_value(card)}")
    print(f"      bot ed_mult  = {card_edition_mult_value(card)}")
    print(f"      bot xmult    = {card_xmult_value(card)}")
    print(f"      bot ed_xmult = {card_edition_xmult_value(card)}")


def run_test(client, label, seed, hand, play_count=None):
    if play_count is None:
        play_count = len(hand)

    print(f"\n{'='*60}")
    print(f"TEST: {label}")
    print(f"{'='*60}")

    state = setup_game(client, seed, card_configs=hand)
    hand_cards = state.get("hand", {}).get("cards", [])
    jokers = state.get("jokers", {}).get("cards", [])

    play_indices = list(range(len(hand_cards) - play_count, len(hand_cards)))
    played = [hand_cards[i] for i in play_indices]
    held = [c for j, c in enumerate(hand_cards) if j not in set(play_indices)]

    print(f"  Played cards:")
    for c in played:
        dump_card(c)
    print(f"  Held cards: {len(held)}")

    hand_name = classify_hand(played)
    scoring = _scoring_cards_for(hand_name, played)

    print(f"  Hand: {hand_name}")
    print(f"  Scoring cards ({len(scoring)}):")
    for c in scoring:
        dump_card(c)

    detail = score_hand_detailed(
        hand_name, scoring,
        hand_levels=state.get("hands", {}),
        jokers=jokers,
        played_cards=played,
        held_cards=held,
        money=state.get("money", 0),
        discards_left=state.get("round", {}).get("discards_left", 0),
        hands_left=state.get("round", {}).get("hands_left", 4),
        joker_limit=state.get("jokers", {}).get("limit", 5),
    )

    pre_chips = state.get("round", {}).get("chips", 0)
    print(f"  Base: {detail['base_chips']}/{detail['base_mult']}")
    print(f"  Pre-joker: {detail['pre_joker_chips']}/{detail['pre_joker_mult']:.1f}")
    print(f"  Bot estimate: {detail['total']}")

    try:
        new_state = client.call("play", {"cards": play_indices})
    except APIError as e:
        print(f"  Play failed: {e.message}")
        return None

    post_chips = new_state.get("round", {}).get("chips", 0)
    actual = post_chips - pre_chips
    diff = actual - detail["total"]

    print(f"  Actual score: {actual}")
    print(f"  Diff: {diff:+d}  {'MATCH' if diff == 0 else 'MISMATCH'}")

    return {"label": label, "est": detail["total"], "actual": actual, "diff": diff}


def main():
    parser = argparse.ArgumentParser()
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

    # 1. Baseline: plain pair of Kings
    results.append(run_test(client, "Baseline: plain Kings pair", "MH1",
        [{"key": "S_K"}, {"key": "H_K"}, {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"}]))

    # 2. MULT enhancement only on one King
    results.append(run_test(client, "MULT enhancement on one King", "MH2",
        [{"key": "S_K", "enhancement": "MULT"}, {"key": "H_K"}, {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"}]))

    # 3. HOLO edition only on one King
    results.append(run_test(client, "HOLO edition on one King", "MH3",
        [{"key": "S_K", "edition": "HOLO"}, {"key": "H_K"}, {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"}]))

    # 4. MULT + HOLO on same King (the collision test)
    results.append(run_test(client, "MULT + HOLO on same King", "MH4",
        [{"key": "S_K", "enhancement": "MULT", "edition": "HOLO"}, {"key": "H_K"}, {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"}]))

    # 5. Both Kings MULT + HOLO
    results.append(run_test(client, "Both Kings MULT + HOLO", "MH5",
        [{"key": "S_K", "enhancement": "MULT", "edition": "HOLO"}, {"key": "H_K", "enhancement": "MULT", "edition": "HOLO"}, {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"}]))

    # 6. MULT on non-scoring card (kicker) — should not contribute mult
    results.append(run_test(client, "MULT on non-scoring kicker", "MH6",
        [{"key": "S_K"}, {"key": "H_K"}, {"key": "D_3", "enhancement": "MULT"}, {"key": "C_4"}, {"key": "S_5"}]))

    # ===================================================================
    print(f"\n\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    for r in results:
        if r is None:
            continue
        status = "MATCH" if r["diff"] == 0 else f"MISMATCH({r['diff']:+d})"
        print(f"  {r['label']:55s} est={r['est']:>8d} actual={r['actual']:>8d} {status}")


if __name__ == "__main__":
    main()
