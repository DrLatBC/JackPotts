"""Integration tests for smaller-issue jokers: Card Sharp, Fortune Teller, Ramen, Joker Stencil.

These jokers consistently over-estimate in batch 048. Each test isolates the joker
against a live Balatro instance to confirm root cause.

Usage:
    python test_smaller_issues.py [--port PORT]
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
from balatro_bot.joker_effects.parsers import parse_effect_value, _ability, _ab_mult, _ab_xmult


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


def setup_game(client, seed, joker_keys=None, card_configs=None, hands_left=None):
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

    if hands_left is not None:
        try:
            client.call("set", {"hands": hands_left})
        except APIError as e:
            print(f"  FAILED to set hands={hands_left}: {e.message}")

    time.sleep(0.3)
    return client.call("gamestate")


def _score_and_play(client, state, hand_size):
    """Score the last `hand_size` cards in hand, play them, return (est, actual, detail)."""
    jokers = state.get("jokers", {}).get("cards", [])
    hand_cards = state.get("hand", {}).get("cards", [])
    hl = state.get("round", {}).get("hands_left", "?")

    play_indices = list(range(len(hand_cards) - hand_size, len(hand_cards)))
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
        hands_left=hl,
        joker_limit=state.get("jokers", {}).get("limit", 5),
    )

    pre_chips = state.get("round", {}).get("chips", 0)

    try:
        new_state = client.call("play", {"cards": play_indices})
    except APIError as e:
        print(f"  Play failed: {e.message}")
        return None, None, detail

    post_chips = new_state.get("round", {}).get("chips", 0)
    actual = post_chips - pre_chips
    return detail["total"], actual, detail


def run_test(client, label, seed, joker_keys, hands_left=None, hand=None):
    if hand is None:
        hand = [{"key": "S_K"}, {"key": "H_K"}, {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"}]

    print(f"\n{'='*60}")
    print(f"TEST: {label}")
    print(f"{'='*60}")

    state = setup_game(client, seed, joker_keys=joker_keys, card_configs=hand, hands_left=hands_left)
    jokers = state.get("jokers", {}).get("cards", [])
    hand_cards = state.get("hand", {}).get("cards", [])
    hl = state.get("round", {}).get("hands_left", "?")

    print(f"  hands_left={hl}")
    print(f"  Jokers:")
    for j in jokers:
        ab = j.get("value", {}).get("ability", {})
        effect = j.get("value", {}).get("effect", "")[:120]
        print(f"    {j.get('key'):20s} ability={ab}")
        print(f"    {'':20s} effect={effect}")

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
        hands_left=hl,
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

    return {"label": label, "est": detail["total"], "actual": actual, "diff": diff}


# ---------------------------------------------------------------------------
# Card Sharp: multi-play test (play same hand type twice in one round)
# ---------------------------------------------------------------------------

def run_test_card_sharp(client, seed):
    """Test Card Sharp by playing Pair twice in the same round.

    Play 1: Pair — Card Sharp should NOT trigger (first play of this type)
    Play 2: Pair — Card Sharp SHOULD trigger (second play of same type)
    Returns list of result dicts.
    """
    print(f"\n{'='*60}")
    print("TEST: Card Sharp — multi-play (Pair x2)")
    print(f"{'='*60}")

    # Need enough hand cards for 2 plays. Add 2 pairs worth of cards.
    hand = [
        {"key": "S_K"}, {"key": "H_K"}, {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"},
        {"key": "S_Q"}, {"key": "H_Q"}, {"key": "D_7"}, {"key": "C_8"}, {"key": "S_9"},
    ]
    state = setup_game(client, seed, joker_keys=["j_card_sharp"], card_configs=hand)

    jokers = state.get("jokers", {}).get("cards", [])
    hand_cards = state.get("hand", {}).get("cards", [])

    print(f"  Hand size: {len(hand_cards)}")
    print(f"  Jokers:")
    for j in jokers:
        ab = j.get("value", {}).get("ability", {})
        effect = j.get("value", {}).get("effect", "")[:120]
        print(f"    {j.get('key'):20s} ability={ab}")
        print(f"    {'':20s} effect={effect}")

    # Dump hand_levels for Pair to see played_this_round
    pair_hl = state.get("hands", {}).get("Pair", {})
    print(f"  Pair hand_levels BEFORE play 1: {pair_hl}")

    results = []

    # --- Play 1: Kings pair (last 10 cards are ours; first 5 = first pair set) ---
    # Added cards appear at end of hand. Play indices for first 5 added cards.
    added_start = len(hand_cards) - len(hand)
    play1_indices = list(range(added_start, added_start + 5))
    played1 = [hand_cards[i] for i in play1_indices]
    held1 = [c for j, c in enumerate(hand_cards) if j not in set(play1_indices)]

    hand_name1 = classify_hand(played1)
    scoring1 = _scoring_cards_for(hand_name1, played1)

    detail1 = score_hand_detailed(
        hand_name1, scoring1,
        hand_levels=state.get("hands", {}),
        jokers=jokers,
        played_cards=played1,
        held_cards=held1,
        money=state.get("money", 0),
        discards_left=state.get("round", {}).get("discards_left", 0),
        hands_left=state.get("round", {}).get("hands_left", 4),
        joker_limit=state.get("jokers", {}).get("limit", 5),
    )

    pre_chips1 = state.get("round", {}).get("chips", 0)
    print(f"\n  Play 1: {hand_name1} (should NOT trigger Card Sharp x3)")
    print(f"    Bot estimate: {detail1['total']}")
    for entry in detail1.get("joker_contributions", []):
        jlabel, dc, dm = entry[0], entry[1], entry[2]
        xm = entry[3] if len(entry) > 3 else 1.0
        parts = []
        if dc: parts.append(f"+{dc:.0f}c")
        if xm > 1.01 or xm < 0.99: parts.append(f"x{xm:.2f}")
        elif dm: parts.append(f"+{dm:.1f}m")
        if parts: print(f"      {jlabel}: {', '.join(parts)}")

    try:
        new_state1 = client.call("play", {"cards": play1_indices})
    except APIError as e:
        print(f"  Play 1 failed: {e.message}")
        return results

    post_chips1 = new_state1.get("round", {}).get("chips", 0)
    actual1 = post_chips1 - pre_chips1
    diff1 = actual1 - detail1["total"]
    print(f"    Actual: {actual1}, Diff: {diff1:+d}  {'MATCH' if diff1 == 0 else 'MISMATCH'}")
    results.append({"label": "Card Sharp: play 1 (should NOT fire)", "est": detail1["total"], "actual": actual1, "diff": diff1})

    # Wait for next hand
    time.sleep(0.5)
    state2 = wait_for_state(client, ["SELECTING_HAND"])

    # Dump hand_levels for Pair after play 1
    pair_hl2 = state2.get("hands", {}).get("Pair", {})
    print(f"\n  Pair hand_levels AFTER play 1: {pair_hl2}")

    jokers2 = state2.get("jokers", {}).get("cards", [])
    hand_cards2 = state2.get("hand", {}).get("cards", [])

    # --- Play 2: Queens pair (from redrawn hand, add new pair) ---
    # After play 1, we drew new cards. Add a fresh pair for play 2.
    for cfg in [{"key": "S_A"}, {"key": "H_A"}, {"key": "D_6"}, {"key": "C_2"}, {"key": "S_7"}]:
        try:
            client.call("add", cfg)
        except APIError as e:
            print(f"  FAILED card {cfg['key']}: {e.message}")

    time.sleep(0.3)
    state2 = client.call("gamestate")
    jokers2 = state2.get("jokers", {}).get("cards", [])
    hand_cards2 = state2.get("hand", {}).get("cards", [])

    # Play the last 5 added cards (Aces pair)
    play2_indices = list(range(len(hand_cards2) - 5, len(hand_cards2)))
    played2 = [hand_cards2[i] for i in play2_indices]
    held2 = [c for j, c in enumerate(hand_cards2) if j not in set(play2_indices)]

    hand_name2 = classify_hand(played2)
    scoring2 = _scoring_cards_for(hand_name2, played2)

    detail2 = score_hand_detailed(
        hand_name2, scoring2,
        hand_levels=state2.get("hands", {}),
        jokers=jokers2,
        played_cards=played2,
        held_cards=held2,
        money=state2.get("money", 0),
        discards_left=state2.get("round", {}).get("discards_left", 0),
        hands_left=state2.get("round", {}).get("hands_left", 3),
        joker_limit=state2.get("jokers", {}).get("limit", 5),
    )

    pre_chips2 = state2.get("round", {}).get("chips", 0)
    print(f"\n  Play 2: {hand_name2} (SHOULD trigger Card Sharp x3)")
    print(f"    Bot estimate: {detail2['total']}")
    for entry in detail2.get("joker_contributions", []):
        jlabel, dc, dm = entry[0], entry[1], entry[2]
        xm = entry[3] if len(entry) > 3 else 1.0
        parts = []
        if dc: parts.append(f"+{dc:.0f}c")
        if xm > 1.01 or xm < 0.99: parts.append(f"x{xm:.2f}")
        elif dm: parts.append(f"+{dm:.1f}m")
        if parts: print(f"      {jlabel}: {', '.join(parts)}")

    try:
        new_state2 = client.call("play", {"cards": play2_indices})
    except APIError as e:
        print(f"  Play 2 failed: {e.message}")
        return results

    post_chips2 = new_state2.get("round", {}).get("chips", 0)
    actual2 = post_chips2 - pre_chips2
    diff2 = actual2 - detail2["total"]
    print(f"    Actual: {actual2}, Diff: {diff2:+d}  {'MATCH' if diff2 == 0 else 'MISMATCH'}")
    results.append({"label": "Card Sharp: play 2 (SHOULD fire x3)", "est": detail2["total"], "actual": actual2, "diff": diff2})

    return results


# ---------------------------------------------------------------------------
# Ramen: test with and without discards
# ---------------------------------------------------------------------------

def run_test_ramen_after_discard(client, seed):
    """Test Ramen after discarding to see if decay value is parsed correctly."""
    print(f"\n{'='*60}")
    print("TEST: Ramen — after 1 discard (decayed value)")
    print(f"{'='*60}")

    hand = [{"key": "S_K"}, {"key": "H_K"}, {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"}]

    try:
        client.call("menu")
    except APIError:
        pass
    time.sleep(0.5)
    client.call("start", {"deck": "RED", "stake": "WHITE", "seed": seed})
    state = wait_for_state(client, ["SELECTING_HAND"])

    # Sell starting jokers
    for i in range(state.get("jokers", {}).get("count", 0)):
        try:
            client.call("sell", {"joker": 0})
        except APIError:
            pass

    # Add Ramen
    try:
        client.call("add", {"key": "j_ramen"})
    except APIError as e:
        print(f"  FAILED: {e.message}")

    # Discard once to decay Ramen (5 cards = -0.01 * 5 = -0.05 from X2.0)
    state = client.call("gamestate")
    hc = state.get("hand", {}).get("cards", [])
    if hc:
        try:
            client.call("discard", {"cards": list(range(min(len(hc), 5)))})
            time.sleep(0.3)
        except APIError:
            pass

    # Discard again
    state = client.call("gamestate")
    hc = state.get("hand", {}).get("cards", [])
    if hc:
        try:
            client.call("discard", {"cards": list(range(min(len(hc), 5)))})
            time.sleep(0.3)
        except APIError:
            pass

    # Add our test hand
    for cfg in hand:
        try:
            client.call("add", cfg)
        except APIError as e:
            print(f"  FAILED card {cfg['key']}: {e.message}")

    time.sleep(0.3)
    state = client.call("gamestate")

    jokers = state.get("jokers", {}).get("cards", [])
    hand_cards = state.get("hand", {}).get("cards", [])
    hl = state.get("round", {}).get("hands_left", "?")

    print(f"  Jokers (after discards):")
    for j in jokers:
        ab = j.get("value", {}).get("ability", {})
        effect = j.get("value", {}).get("effect", "")[:120]
        parsed = parse_effect_value(effect)
        print(f"    {j.get('key'):20s} ability={ab}")
        print(f"    {'':20s} effect={effect}")
        print(f"    {'':20s} parsed={parsed}")

    # Play last 5 cards
    play_indices = list(range(len(hand_cards) - len(hand), len(hand_cards)))
    played = [hand_cards[i] for i in play_indices]
    held = [c for j, c in enumerate(hand_cards) if j not in set(play_indices)]

    hand_name = classify_hand(played)
    scoring = _scoring_cards_for(hand_name, played)

    detail = score_hand_detailed(
        hand_name, scoring,
        hand_levels=state.get("hands", {}),
        jokers=jokers,
        played_cards=played,
        held_cards=held,
        money=state.get("money", 0),
        discards_left=state.get("round", {}).get("discards_left", 0),
        hands_left=hl,
        joker_limit=state.get("jokers", {}).get("limit", 5),
    )

    pre_chips = state.get("round", {}).get("chips", 0)
    print(f"  Hand: {hand_name}")
    print(f"  Bot estimate: {detail['total']}")

    try:
        new_state = client.call("play", {"cards": play_indices})
    except APIError as e:
        print(f"  Play failed: {e.message}")
        return None

    post_chips = new_state.get("round", {}).get("chips", 0)
    actual = post_chips - pre_chips
    diff = actual - detail["total"]
    print(f"  Actual: {actual}, Diff: {diff:+d}  {'MATCH' if diff == 0 else 'MISMATCH'}")

    return {"label": "Ramen: after discards (decayed)", "est": detail["total"], "actual": actual, "diff": diff}


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

    # ===================================================================
    # CARD SHARP — off-by-one in played_this_round
    # ===================================================================
    # Single play test: first Pair should NOT trigger x3
    results.append(run_test(client, "Card Sharp: first Pair (should NOT fire x3)", "CS1",
        ["j_card_sharp"],
        hand=[{"key": "S_K"}, {"key": "H_K"}, {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"}]))

    # Multi-play test: play Pair twice, first should NOT trigger, second SHOULD
    cs_results = run_test_card_sharp(client, "CS2")
    results.extend(cs_results)

    # ===================================================================
    # FORTUNE TELLER — parser issue with +mult value
    # ===================================================================
    # Base case: 0 tarots used, should give +0 mult
    results.append(run_test(client, "Fortune Teller: base (0 tarots)", "FT1",
        ["j_fortune_teller"],
        hand=[{"key": "S_K"}, {"key": "H_K"}, {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"}]))

    # With another joker to establish baseline scoring
    results.append(run_test(client, "Fortune Teller + Gros Michel: base", "FT2",
        ["j_fortune_teller", "j_gros_michel"],
        hand=[{"key": "S_K"}, {"key": "H_K"}, {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"}]))

    # ===================================================================
    # RAMEN — parser grabs wrong number from decay text
    # ===================================================================
    # Fresh Ramen (no discards) — should be X2.0
    results.append(run_test(client, "Ramen: fresh (X2.0)", "RM1",
        ["j_ramen"],
        hand=[{"key": "S_K"}, {"key": "H_K"}, {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"}]))

    # Ramen after discards — decayed value
    r = run_test_ramen_after_discard(client, "RM2")
    if r:
        results.append(r)

    # ===================================================================
    # JOKER STENCIL — empty slot counting
    # ===================================================================
    # 1 joker in 5 slots: 4 empty + 1 stencil = x5
    results.append(run_test(client, "Stencil: alone (4 empty + self = x5?)", "ST1",
        ["j_stencil"],
        hand=[{"key": "S_K"}, {"key": "H_K"}, {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"}]))

    # 2 jokers in 5 slots: 3 empty + 1 stencil = x4
    results.append(run_test(client, "Stencil + Gros Michel (3 empty + self = x4?)", "ST2",
        ["j_stencil", "j_gros_michel"],
        hand=[{"key": "S_K"}, {"key": "H_K"}, {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"}]))

    # 3 jokers in 5 slots: 2 empty + 1 stencil = x3
    results.append(run_test(client, "Stencil + 2 jokers (2 empty + self = x3?)", "ST3",
        ["j_stencil", "j_gros_michel", "j_jolly"],
        hand=[{"key": "S_K"}, {"key": "H_K"}, {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"}]))

    # Full roster: 0 empty + 1 stencil = x1 (noop)
    results.append(run_test(client, "Stencil: full roster (0 empty + self = x1?)", "ST4",
        ["j_stencil", "j_gros_michel", "j_jolly", "j_zany", "j_sly"],
        hand=[{"key": "S_K"}, {"key": "H_K"}, {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"}]))

    # ===================================================================
    # SUMMARY
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
