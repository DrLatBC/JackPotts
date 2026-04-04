"""Test per-card joker effects: do they fire on unscored played cards?
Also test Splash+Pareidolia face card mult.

Tests use plain (no edition) jokers to isolate the mechanic from joker editions.

Usage:
    python test_percard_scoring.py [--port PORT]
"""

import argparse
import logging
import sys
import time

sys.path.insert(0, "src")

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


def run_test(client, label, seed, joker_keys, hand_configs, play_count=None):
    print(f"\n{'='*60}")
    print(f"TEST: {label}")
    print(f"{'='*60}")

    state = setup_game(client, seed, joker_keys=joker_keys, card_configs=hand_configs)
    jokers = state.get("jokers", {}).get("cards", [])
    hand_cards = state.get("hand", {}).get("cards", [])

    print(f"  Jokers:")
    for j in jokers:
        mod = j.get("modifier", {})
        if not isinstance(mod, dict): mod = {}
        rarity = j.get("value", {}).get("rarity", "?")
        ed = mod.get("edition", "-")
        ab = j.get("value", {}).get("ability", {})
        print(f"    {j.get('key'):20s} rarity={str(rarity):10s} ed={ed:12s} ability={ab}")

    n = len(hand_configs)
    if play_count is None:
        play_count = n
    play_indices = list(range(len(hand_cards) - n, len(hand_cards) - n + play_count))
    played = [hand_cards[i] for i in play_indices]
    held = [c for j, c in enumerate(hand_cards) if j not in set(play_indices)]

    hand_name = classify_hand(played)
    joker_key_set = {j.get("key") for j in jokers}
    scoring = played if "j_splash" in joker_key_set else _scoring_cards_for(hand_name, played)

    print(f"  Hand: {hand_name}")
    print(f"  Played ({len(played)}):")
    for i, c in enumerate(played):
        r = c.get("value", {}).get("rank", "?")
        s = c.get("value", {}).get("suit", "?")
        in_scoring = c in scoring
        print(f"    {r}{s} {'(SCORING)' if in_scoring else '(not scoring)'}")
    print(f"  Scoring: {len(scoring)} cards")

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
    if diff != 0 and detail["post_joker_chips"]:
        bot_mult = detail["post_joker_mult"]
        actual_mult = actual / detail["post_joker_chips"]
        print(f"  Bot mult: {bot_mult:.2f}  Actual mult: {actual_mult:.2f}  Mult gap: {actual_mult - bot_mult:+.2f}")

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

    # =====================================================================
    # SMILEY FACE: +5 mult per face card scored
    # Does it fire on non-scoring played cards?
    # =====================================================================

    # Test 1: Smiley, 2 face cards scoring, 0 non-face non-scoring
    # Pair of Kings, play only 2 cards — all played = all scoring
    results.append(run_test(client, "Smiley: 2 Kings played (all scoring)", "SM1",
        ["j_smiley"],
        [{"key": "S_K"}, {"key": "H_K"}]))

    # Test 2: Smiley, 2 face cards scoring, 3 non-face pad
    # Pair of Kings + 3,4,5 — only Kings score, pad doesn't
    results.append(run_test(client, "Smiley: 2 Kings + 3 non-face pad (5 played)", "SM2",
        ["j_smiley"],
        [{"key": "S_K"}, {"key": "H_K"}, {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"}]))

    # Test 3: Smiley, 2 face cards scoring, 3 FACE pad (non-scoring)
    # Pair of Kings + Q,J,Q — pad cards ARE face but not scoring
    results.append(run_test(client, "Smiley: 2 Kings + 3 face pad (QJQ, 5 played)", "SM3",
        ["j_smiley"],
        [{"key": "S_K"}, {"key": "H_K"}, {"key": "D_Q"}, {"key": "C_J"}, {"key": "S_Q"}]))

    # Test 4: If test 2 == test 3, Smiley only fires on scoring cards.
    #         If test 3 > test 2, Smiley fires on non-scoring face cards too.

    # =====================================================================
    # SCARY FACE: +30 chips per face card scored
    # Same question — does it fire on non-scoring played cards?
    # =====================================================================

    results.append(run_test(client, "Scary: 2 Kings played (all scoring)", "SC1",
        ["j_scary_face"],
        [{"key": "S_K"}, {"key": "H_K"}]))

    results.append(run_test(client, "Scary: 2 Kings + 3 non-face pad", "SC2",
        ["j_scary_face"],
        [{"key": "S_K"}, {"key": "H_K"}, {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"}]))

    results.append(run_test(client, "Scary: 2 Kings + 3 face pad (QJQ)", "SC3",
        ["j_scary_face"],
        [{"key": "S_K"}, {"key": "H_K"}, {"key": "D_Q"}, {"key": "C_J"}, {"key": "S_Q"}]))

    # =====================================================================
    # ODD TODD: +31 chips per odd rank scored
    # Does it fire on non-scoring odd cards?
    # =====================================================================

    results.append(run_test(client, "Odd Todd: 2 Kings + 3 even pad (no odd non-scoring)", "OT1",
        ["j_odd_todd"],
        [{"key": "S_K"}, {"key": "H_K"}, {"key": "D_2"}, {"key": "C_4"}, {"key": "S_6"}]))

    results.append(run_test(client, "Odd Todd: 2 Kings + 3 odd pad (A,3,5 non-scoring)", "OT2",
        ["j_odd_todd"],
        [{"key": "S_K"}, {"key": "H_K"}, {"key": "D_A"}, {"key": "C_3"}, {"key": "S_5"}]))

    # =====================================================================
    # FIBONACCI: +8 mult per Fibonacci rank scored (A,2,3,5,8)
    # Does it fire on non-scoring Fibonacci cards?
    # =====================================================================

    results.append(run_test(client, "Fibonacci: 2 Kings + 3 non-Fib pad (4,6,7)", "FIB1",
        ["j_fibonacci"],
        [{"key": "S_K"}, {"key": "H_K"}, {"key": "D_4"}, {"key": "C_6"}, {"key": "S_7"}]))

    results.append(run_test(client, "Fibonacci: 2 Kings + 3 Fib pad (A,3,5)", "FIB2",
        ["j_fibonacci"],
        [{"key": "S_K"}, {"key": "H_K"}, {"key": "D_A"}, {"key": "C_3"}, {"key": "S_5"}]))

    # =====================================================================
    # SPLASH + PAREIDOLIA: all cards score, all are face
    # Is there extra mult beyond what Splash alone gives?
    # =====================================================================

    # Baseline: no jokers
    results.append(run_test(client, "Baseline: Pair KK + 3,4,5 (no jokers)", "SP0",
        [],
        [{"key": "S_K"}, {"key": "H_K"}, {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"}]))

    # Splash only — all 5 cards score
    results.append(run_test(client, "Splash only: Pair KK + 3,4,5", "SP1",
        ["j_splash"],
        [{"key": "S_K"}, {"key": "H_K"}, {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"}]))

    # Pareidolia only — all cards are face (but only 2 score)
    results.append(run_test(client, "Pareidolia only: Pair KK + 3,4,5", "SP2",
        ["j_pareidolia"],
        [{"key": "S_K"}, {"key": "H_K"}, {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"}]))

    # Splash + Pareidolia — all cards score AND all are face
    results.append(run_test(client, "Splash + Pareidolia: Pair KK + 3,4,5", "SP3",
        ["j_splash", "j_pareidolia"],
        [{"key": "S_K"}, {"key": "H_K"}, {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"}]))

    # Splash + Pareidolia + Smiley — stacks with per-face-card joker
    results.append(run_test(client, "Splash + Pareidolia + Smiley: Pair KK + 3,4,5", "SP4",
        ["j_splash", "j_pareidolia", "j_smiley"],
        [{"key": "S_K"}, {"key": "H_K"}, {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"}]))

    # =====================================================================
    # Summary
    # =====================================================================
    print(f"\n\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    for r in results:
        if r is None:
            continue
        status = "MATCH" if r["diff"] == 0 else f"MISMATCH({r['diff']:+d})"
        print(f"  {r['label']:55s} est={r['est']:>8d} actual={r['actual']:>8d} {status}")

    # Key comparisons
    print(f"\n  KEY COMPARISONS:")
    def get(lbl):
        return next((r for r in results if r and lbl in r["label"]), None)

    sm2, sm3 = get("Smiley: 2 Kings + 3 non-face"), get("Smiley: 2 Kings + 3 face pad")
    if sm2 and sm3:
        print(f"  Smiley non-face pad vs face pad: {sm2['actual']} vs {sm3['actual']}", end="")
        diff_sm = sm3['actual'] - sm2['actual']
        print(f" — {'SAME (only scoring cards)' if diff_sm == 0 else f'DIFFERENT (+{diff_sm}, fires on unscored)'}")

    sc2, sc3 = get("Scary: 2 Kings + 3 non-face"), get("Scary: 2 Kings + 3 face pad")
    if sc2 and sc3:
        print(f"  Scary non-face pad vs face pad:  {sc2['actual']} vs {sc3['actual']}", end="")
        diff_sc = sc3['actual'] - sc2['actual']
        print(f" — {'SAME' if diff_sc == 0 else f'DIFFERENT (+{diff_sc})'}")

    ot1, ot2 = get("Odd Todd: 2 Kings + 3 even"), get("Odd Todd: 2 Kings + 3 odd")
    if ot1 and ot2:
        print(f"  Odd Todd even pad vs odd pad:    {ot1['actual']} vs {ot2['actual']}", end="")
        diff_ot = ot2['actual'] - ot1['actual']
        print(f" — {'SAME' if diff_ot == 0 else f'DIFFERENT (+{diff_ot})'}")

    f1, f2 = get("Fibonacci: 2 Kings + 3 non-Fib"), get("Fibonacci: 2 Kings + 3 Fib")
    if f1 and f2:
        print(f"  Fibonacci non-Fib pad vs Fib:    {f1['actual']} vs {f2['actual']}", end="")
        diff_f = f2['actual'] - f1['actual']
        print(f" — {'SAME' if diff_f == 0 else f'DIFFERENT (+{diff_f})'}")

    sp0, sp1, sp2, sp3 = get("Baseline:"), get("Splash only:"), get("Pareidolia only:"), get("Splash + Pareidolia: Pair")
    if sp0 and sp1 and sp2 and sp3:
        print(f"  Baseline={sp0['actual']}  Splash={sp1['actual']}  Pareidolia={sp2['actual']}  Splash+Pareidolia={sp3['actual']}")
        if sp3["actual"] > sp1["actual"]:
            diff_sp = sp3['actual'] - sp1['actual']
            print(f"  >> Splash+Pareidolia gives +{diff_sp} over Splash alone")


if __name__ == "__main__":
    main()
