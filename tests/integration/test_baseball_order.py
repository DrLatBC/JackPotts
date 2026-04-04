"""Test Baseball Card ordering and xMult interactions.

Baseball applies x1.5 after each Uncommon joker fires. With multiple Uncommon
jokers and xMult jokers, order matters enormously. This test verifies the bot
gets the multiplication chain right.

Usage:
    python test_baseball_order.py [--port PORT]
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
from balatro_bot.cards import card_chip_value, _modifier


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


def run_test(client, label, seed, joker_keys, hand=None):
    if hand is None:
        hand = [{"key": "S_K"}, {"key": "H_K"}, {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"}]

    print(f"\n{'='*60}")
    print(f"TEST: {label}")
    print(f"{'='*60}")

    state = setup_game(client, seed, joker_keys=joker_keys, card_configs=hand)
    jokers = state.get("jokers", {}).get("cards", [])
    hand_cards = state.get("hand", {}).get("cards", [])

    print(f"  Joker order (left to right):")
    for i, j in enumerate(jokers):
        rarity = j.get("value", {}).get("rarity")
        rarity_str = {1: "Common", 2: "Uncommon", 3: "Rare", 4: "Legendary"}.get(rarity, str(rarity))
        mod = j.get("modifier", {})
        if not isinstance(mod, dict): mod = {}
        ed = mod.get("edition", "-")
        print(f"    [{i}] {j.get('key'):20s} rarity={rarity_str:10s} ed={ed}")

    play_indices = list(range(len(hand_cards) - len(hand), len(hand_cards)))
    played = [hand_cards[i] for i in play_indices]
    held = [c for j, c in enumerate(hand_cards) if j not in set(play_indices)]

    hand_name = classify_hand(played)
    joker_key_set = {j.get("key") for j in jokers}
    scoring = played if "j_splash" in joker_key_set else _scoring_cards_for(hand_name, played)

    detail = score_hand_detailed(
        hand_name, scoring,
        hand_levels=state.get("hands", {}),
        jokers=jokers,
        played_cards=played,
        held_cards=held,
        money=state.get("money", 0),
        discards_left=state.get("round", {}).get("discards_left", 0),
        hands_left=state.get("round", {}).get("hands_left", 1),
        joker_limit=state.get("jokers", {}).get("limit", 5),
    )

    pre_chips = state.get("round", {}).get("chips", 0)
    print(f"  Hand: {hand_name}")
    print(f"  Base: {detail['base_chips']}/{detail['base_mult']}")
    print(f"  Pre-joker: {detail['pre_joker_chips']}/{detail['pre_joker_mult']:.1f}")
    for entry in detail.get("joker_contributions", []):
        jlabel, dc, dm = entry[0], entry[1], entry[2]
        xm = entry[3] if len(entry) > 3 else 1.0
        parts = []
        if dc: parts.append(f"+{dc:.0f}c")
        if xm > 1.01 or xm < 0.99: parts.append(f"x{xm:.2f}")
        elif dm: parts.append(f"+{dm:.1f}m")
        if parts: print(f"    {jlabel}: {', '.join(parts)}")
    print(f"  Post-joker: {detail['post_joker_chips']}/{detail['post_joker_mult']:.1f}")
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
    if diff != 0:
        actual_mult = actual / detail["post_joker_chips"] if detail["post_joker_chips"] else 0
        print(f"  Bot mult: {detail['post_joker_mult']:.2f}  Actual mult: {actual_mult:.2f}")

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

    # All tests use Pair of Kings + junk so base is constant and predictable.
    # Pair base: 10/2, Kings: +10+10 chips. Pre-joker: 30 chips / 2 mult.

    # ---------------------------------------------------------------
    # 1. Baseball + 2 Uncommon +mult jokers (Jolly +8m, Scary Face +30c/face)
    #    Jolly is Uncommon, triggers on Pair → +8 mult, then x1.5
    #    Scary Face is Uncommon, +30 chips per face (2 Kings) → +60c, then x1.5
    #    Expected: base 30c/2m → Jolly +8m=10m → x1.5=15m → Scary +60c=90c → x1.5=22.5m
    #    Total: 90 * 22.5 = 2025
    # ---------------------------------------------------------------
    results.append(run_test(client, "Baseball + Jolly(U) + Scary(U): 2 uncommon +mult", "BO1",
        ["j_jolly", "j_scary_face", "j_baseball"]))

    # ---------------------------------------------------------------
    # 2. Baseball + 3 Uncommon jokers — triple x1.5 chain
    #    Jolly(U) + Fibonacci(U) + Scary Face(U) + Baseball
    #    Fib: A is not in hand so won't trigger. Using face cards.
    #    Let's use: Jolly(U, +8m on Pair) + Scary(U, +30c/face) + Droll(U, +10m on Flush)
    #    Droll won't trigger (not flush). So just Jolly + Scary with 3rd uncommon noop.
    # ---------------------------------------------------------------
    results.append(run_test(client, "Baseball + Jolly(U) + Scary(U) + Droll(U): 3 uncommon", "BO2",
        ["j_jolly", "j_scary_face", "j_droll", "j_baseball"]))

    # ---------------------------------------------------------------
    # 3. Baseball + Uncommon xMult joker (Duo — x2 on Pair, Uncommon)
    #    Duo fires x2 on Pair, then Baseball x1.5 → effective x3.0
    #    base 30c/2m → Duo x2=4m → Baseball x1.5=6m → 30*6=180
    # ---------------------------------------------------------------
    results.append(run_test(client, "Baseball + Duo(U, x2 Pair): xmult chain", "BO3",
        ["j_duo", "j_baseball"]))

    # ---------------------------------------------------------------
    # 4. Baseball + TWO Uncommon xMult jokers (Duo x2 + Tribe x2 on Flush)
    #    Only Duo triggers (Pair not Flush). But Tribe is still Uncommon.
    #    Does Baseball x1.5 fire on Tribe even though Tribe's effect is a noop?
    # ---------------------------------------------------------------
    results.append(run_test(client, "Baseball + Duo(U) + Tribe(U, no trigger): inactive uncommon", "BO4",
        ["j_duo", "j_tribe", "j_baseball"]))

    # ---------------------------------------------------------------
    # 5. Baseball + Duo(U) + Jolly(U) — both trigger on Pair
    #    Jolly +8m, then x1.5. Duo x2, then x1.5.
    #    Order: Jolly fires (+8m=10m), x1.5→15m. Duo fires (x2→30m), x1.5→45m.
    #    30 * 45 = 1350
    # ---------------------------------------------------------------
    results.append(run_test(client, "Baseball + Jolly(U) + Duo(U): both trigger Pair", "BO5",
        ["j_jolly", "j_duo", "j_baseball"]))

    # ---------------------------------------------------------------
    # 6. Same as 5 but reverse Jolly/Duo order — tests order sensitivity
    #    Duo fires (x2→4m), x1.5→6m. Jolly fires (+8m=14m), x1.5→21m.
    #    30 * 21 = 630
    #    vs test 5's 1350 — if order matters, these should differ
    # ---------------------------------------------------------------
    results.append(run_test(client, "Baseball + Duo(U) + Jolly(U): reversed order", "BO6",
        ["j_duo", "j_jolly", "j_baseball"]))

    # ---------------------------------------------------------------
    # 7. Polychrome edition on Uncommon joker + Baseball
    #    Jolly(U, POLY) → +8m, then edition x1.5, then Baseball x1.5
    #    Or does Baseball fire before edition? Test to find out.
    # ---------------------------------------------------------------
    results.append(run_test(client, "Baseball + Jolly(U, POLY): edition + baseball order", "BO7",
        [{"key": "j_jolly", "edition": "POLYCHROME"}, "j_baseball"]))

    # ---------------------------------------------------------------
    # 8. Baseball with Common joker that has xmult — verify NO x1.5
    #    Cavendish (Common, x3 Mult) — should NOT get Baseball bonus
    # ---------------------------------------------------------------
    results.append(run_test(client, "Baseball + Cavendish(C, x3): common no trigger", "BO8",
        ["j_cavendish", "j_baseball"]))

    # ---------------------------------------------------------------
    # 9. Cavendish alone — baseline for comparison with test 8
    # ---------------------------------------------------------------
    results.append(run_test(client, "Cavendish alone (baseline)", "BO9",
        ["j_cavendish"]))

    # ---------------------------------------------------------------
    # Summary
    # ---------------------------------------------------------------
    print(f"\n\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    for r in results:
        if r is None:
            continue
        status = "MATCH" if r["diff"] == 0 else f"MISMATCH({r['diff']:+d})"
        print(f"  {r['label']:55s} est={r['est']:>8d} actual={r['actual']:>8d} {status}")

    # Order sensitivity check
    t5 = next((r for r in results if r and "BO5" in r.get("label", "") or "both trigger" in r.get("label", "")), None)
    t6 = next((r for r in results if r and "reversed order" in r.get("label", "")), None)
    if t5 and t6:
        print(f"\n  Order test: Jolly→Duo actual={t5['actual']}  Duo→Jolly actual={t6['actual']}")
        if t5["actual"] != t6["actual"]:
            print(f"  >> ORDER MATTERS (diff={t5['actual'] - t6['actual']})")
        else:
            print(f"  >> Order does NOT matter (same score)")


if __name__ == "__main__":
    main()
