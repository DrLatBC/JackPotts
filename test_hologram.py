"""Integration tests for Hologram scoring accuracy.

Hologram gains X0.25 Mult every time a playing card is added to the deck.
It only applies when x_mult > 1 (i.e., at least 1 card has been added).

Approach: give infinite money, cycle through shops buying Standard packs
to naturally add cards to the deck (triggering Hologram increments).
After each shop phase, play a controlled hand and compare est vs actual.

Usage:
    python test_hologram.py [--port PORT]
"""

import argparse
import math
import sys
import time

sys.path.insert(0, "src")

from balatrobot.cli.client import BalatroClient, APIError
from balatro_bot.domain.scoring.classify import classify_hand, _scoring_cards_for
from balatro_bot.domain.scoring.estimate import score_hand_detailed
from balatro_bot.cards import _modifier
from balatro_bot.joker_effects.parsers import parse_effect_value, _ability, _ab_xmult


# ── helpers ──────────────────────────────────────────────────────

def give_money(client, amount=9999):
    try:
        client.call("set", {"money": amount})
    except APIError:
        pass


def beat_blind_fast(client):
    """Set chips absurdly high and play first 5 cards to instantly win."""
    try:
        client.call("set", {"chips": 999999})
    except APIError:
        pass
    state = client.call("gamestate")
    hc = state.get("hand", {}).get("cards", [])
    if hc:
        try:
            client.call("play", {"cards": list(range(min(5, len(hc))))})
        except APIError:
            pass
    time.sleep(0.5)


def wait_for_state(client, target_states, max_tries=30):
    for _ in range(max_tries):
        state = client.call("gamestate")
        gs = state.get("state", "")
        if gs in target_states:
            return state
        if gs == "BLIND_SELECT":
            client.call("select")
            time.sleep(0.3)
        elif gs == "SHOP":
            client.call("next_round")
            time.sleep(0.3)
        elif gs in ("HAND_PLAYED", "DRAW_TO_HAND", "NEW_ROUND", "ROUND_EVAL"):
            time.sleep(0.3)
        else:
            time.sleep(0.3)
    raise TimeoutError(f"Never reached {target_states}, stuck in {state.get('state')}")


def wait_for_one(client, target_state, max_tries=30):
    """Wait for a single specific state."""
    for _ in range(max_tries):
        state = client.call("gamestate")
        if state.get("state") == target_state:
            return state
        time.sleep(0.3)
    return None


def advance_to_shop(client, max_tries=40):
    """From any state, advance until we reach SHOP."""
    for _ in range(max_tries):
        state = client.call("gamestate")
        gs = state.get("state", "")
        if gs == "SHOP":
            return state
        if gs == "SELECTING_HAND":
            beat_blind_fast(client)
        elif gs == "ROUND_EVAL":
            try:
                client.call("cash_out")
            except APIError:
                pass
            time.sleep(0.3)
        elif gs == "BLIND_SELECT":
            try:
                client.call("select")
            except APIError:
                pass
            time.sleep(0.3)
        elif gs in ("HAND_PLAYED", "DRAW_TO_HAND", "NEW_ROUND"):
            time.sleep(0.3)
        else:
            time.sleep(0.3)
    raise TimeoutError(f"Never reached SHOP")


def advance_to_selecting_hand(client, max_tries=40):
    """From SHOP or BLIND_SELECT, advance to SELECTING_HAND."""
    for _ in range(max_tries):
        state = client.call("gamestate")
        gs = state.get("state", "")
        if gs == "SELECTING_HAND":
            return state
        if gs == "SHOP":
            try:
                client.call("next_round")
            except APIError:
                pass
            time.sleep(0.3)
        elif gs == "BLIND_SELECT":
            try:
                client.call("select")
            except APIError:
                pass
            time.sleep(0.3)
        elif gs in ("HAND_PLAYED", "DRAW_TO_HAND", "NEW_ROUND"):
            time.sleep(0.3)
        else:
            time.sleep(0.3)
    raise TimeoutError(f"Never reached SELECTING_HAND")


def buy_standard_packs(client, state, max_rerolls=10):
    """In SHOP state, reroll until Standard packs appear and buy them.
    Pick all cards from each pack to add them to the deck.
    Returns the number of cards added."""
    cards_added = 0

    for reroll_num in range(max_rerolls + 1):
        state = client.call("gamestate")
        if state.get("state") != "SHOP":
            break

        packs = state.get("packs", {}).get("cards", [])

        for i, pack in enumerate(packs):
            label = pack.get("label", "")
            if "Standard" not in label:
                continue

            # Buy this pack
            print(f"    Buying pack {i}: {label}")
            try:
                client.call("buy", {"pack": i})
            except APIError as e:
                print(f"    Buy failed: {e.message}")
                continue
            time.sleep(0.5)

            # Pick all cards from the opened pack
            for pick_attempt in range(10):
                ps = client.call("gamestate")
                gs = ps.get("state", "")
                if gs == "SHOP":
                    break  # Pack closed
                pack_cards = ps.get("pack", {}).get("cards", [])
                if not pack_cards:
                    if gs in ("SMODS_BOOSTER_OPENED", "STANDARD_PACK"):
                        # Try skipping
                        try:
                            client.call("pack", {"skip": True})
                        except APIError:
                            pass
                        time.sleep(0.3)
                    break

                # Pick first card
                try:
                    client.call("pack", {"card": 0})
                    cards_added += 1
                    print(f"      Picked card from pack (+1, total {cards_added})")
                except APIError:
                    # Maybe need to skip
                    try:
                        client.call("pack", {"skip": True})
                    except APIError:
                        pass
                    break
                time.sleep(0.3)

        # Reroll for more packs
        if reroll_num < max_rerolls:
            give_money(client)
            try:
                client.call("reroll")
            except APIError:
                break
            time.sleep(0.3)

    return cards_added


def dump_hologram(jokers):
    """Print Hologram diagnostic info."""
    holo = next((j for j in jokers if j.get("key") == "j_hologram"), None)
    if not holo:
        print("  [no Hologram]")
        return None
    ab = _ability(holo)
    xmult = _ab_xmult(holo, fallback=1.5)
    effect = holo.get("value", {}).get("effect", "")
    print(f"  Hologram: ability.x_mult={ab.get('x_mult', '?')} "
          f"extra={ab.get('extra', '?')} "
          f"_ab_xmult()={xmult}")
    print(f"  Effect: {effect[:120]}")
    return xmult


def play_controlled_hand(client, card_configs, jokers_state, label="", hand_num=0):
    """Clear hand, add controlled cards, play them, compare est vs actual."""
    # Discard existing hand
    state = client.call("gamestate")
    for _ in range(3):
        hc = state.get("hand", {}).get("cards", [])
        if not hc:
            break
        try:
            client.call("discard", {"cards": list(range(min(len(hc), 5)))})
            time.sleep(0.2)
        except APIError:
            break
        state = client.call("gamestate")

    # Add controlled hand
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
    state = client.call("gamestate")

    jokers = state.get("jokers", {}).get("cards", [])
    hand_cards = state.get("hand", {}).get("cards", [])
    hand_levels = state.get("hands", {})

    play_count = min(5, len(hand_cards))
    if play_count == 0:
        print(f"  No cards to play!")
        return None

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

    holo = next((j for j in jokers if j.get("key") == "j_hologram"), None)
    bot_holo_xmult = _ab_xmult(holo, fallback=1.5) if holo else None

    pre_chips = state.get("round", {}).get("chips", 0)
    prefix = f"  Hand #{hand_num}: " if hand_num else "  "
    print(f"{prefix}{hand_name} = {[c.get('label','?') for c in played]}")
    print(f"{prefix}  Hologram xmult from API: {bot_holo_xmult}")
    print(f"{prefix}  Base: {detail['base_chips']}/{detail['base_mult']}")

    for entry in detail.get("joker_contributions", []):
        jlabel, dc, dm = entry[0], entry[1], entry[2]
        xm = entry[3] if len(entry) > 3 else 1.0
        parts = []
        if dc: parts.append(f"+{dc:.0f}c")
        if xm > 1.01 or xm < 0.99: parts.append(f"x{xm:.2f}")
        elif dm: parts.append(f"+{dm:.1f}m")
        if parts: print(f"{prefix}    {jlabel}: {', '.join(parts)}")

    print(f"{prefix}  Post-joker: {detail['post_joker_chips']}/{detail['post_joker_mult']:.2f}")
    print(f"{prefix}  Bot est: {detail['total']}")

    try:
        new_state = client.call("play", {"cards": play_indices})
    except APIError as e:
        print(f"{prefix}  Play failed: {e.message}")
        return None

    post_chips = new_state.get("round", {}).get("chips", 0)
    actual = post_chips - pre_chips
    diff = actual - detail["total"]

    status = "MATCH" if diff == 0 else "MISMATCH"
    print(f"{prefix}  Actual: {actual}  Diff: {diff:+d}  {status}")

    if diff != 0 and bot_holo_xmult and bot_holo_xmult > 1:
        non_holo_score = detail["total"] / bot_holo_xmult
        if non_holo_score > 0:
            implied = actual / non_holo_score
            print(f"{prefix}  >>> Implied Hologram xmult: {implied:.3f} (bot used {bot_holo_xmult})")

    return {
        "label": label, "est": detail["total"], "actual": actual, "diff": diff,
        "hand": hand_name, "hand_num": hand_num,
        "bot_holo_xmult": bot_holo_xmult,
    }


# ── test runner ──────────────────────────────────────────────────

def run_hologram_tests(client):
    print("\n" + "=" * 60)
    print("HOLOGRAM TESTS — xmult accuracy via natural pack buying")
    print("=" * 60)

    results = []
    pair_hand = [
        {"key": "S_K"}, {"key": "H_K"},
        {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"}
    ]

    # ── Setup: start game with Hologram only ──
    print(f"\n--- Setup: starting game with Hologram ---")
    try:
        client.call("menu")
    except APIError:
        pass
    time.sleep(0.5)
    client.call("start", {"deck": "RED", "stake": "WHITE", "seed": "HOLOTEST"})
    state = wait_for_state(client, ["SELECTING_HAND"])

    # Sell any starting jokers
    for i in range(state.get("jokers", {}).get("count", 0)):
        try:
            client.call("sell", {"joker": 0})
        except APIError:
            pass

    # Add Hologram
    try:
        client.call("add", {"key": "j_hologram"})
    except APIError as e:
        print(f"  FAILED to add Hologram: {e.message}")
        return results

    give_money(client)
    state = client.call("gamestate")
    print(f"  Starting state:")
    xm = dump_hologram(state.get("jokers", {}).get("cards", []))

    # ── Test 1: Fresh Hologram (x1.0) ──
    print(f"\n--- Test 1: Fresh Hologram (should be x1.0, no effect) ---")
    r = play_controlled_hand(client, pair_hand, state, label="Fresh Hologram x1.0", hand_num=1)
    if r: results.append(r)

    # ── Beat blind, go to shop, buy Standard packs ──
    print(f"\n--- Advancing to shop, buying Standard packs ---")
    give_money(client)
    state = advance_to_shop(client)
    give_money(client)
    total_added = buy_standard_packs(client, state, max_rerolls=15)
    print(f"  Total cards added from packs: {total_added}")

    # ── Test 2: After buying packs ──
    state = advance_to_selecting_hand(client)
    give_money(client)
    state = client.call("gamestate")
    print(f"\n--- Test 2: After pack buying ({total_added} cards added) ---")
    xm = dump_hologram(state.get("jokers", {}).get("cards", []))
    expected = 1 + 0.25 * total_added
    print(f"  Expected xmult if all additions counted: {expected}")
    r = play_controlled_hand(client, pair_hand, state, label=f"After {total_added} cards added", hand_num=2)
    if r: results.append(r)

    # ── Buy more packs in another shop cycle ──
    print(f"\n--- Second shop cycle ---")
    give_money(client)
    state = advance_to_shop(client)
    give_money(client)
    added2 = buy_standard_packs(client, state, max_rerolls=15)
    total_added += added2
    print(f"  Cards added this cycle: {added2}, cumulative: {total_added}")

    # ── Test 3: After second round of packs ──
    state = advance_to_selecting_hand(client)
    give_money(client)
    state = client.call("gamestate")
    print(f"\n--- Test 3: After second pack cycle ({total_added} total) ---")
    xm = dump_hologram(state.get("jokers", {}).get("cards", []))
    expected = 1 + 0.25 * total_added
    print(f"  Expected xmult: {expected}")
    r = play_controlled_hand(client, pair_hand, state, label=f"After {total_added} cards (cycle 2)", hand_num=3)
    if r: results.append(r)

    # ── Third cycle ──
    print(f"\n--- Third shop cycle ---")
    give_money(client)
    state = advance_to_shop(client)
    give_money(client)
    added3 = buy_standard_packs(client, state, max_rerolls=15)
    total_added += added3
    print(f"  Cards added this cycle: {added3}, cumulative: {total_added}")

    state = advance_to_selecting_hand(client)
    give_money(client)
    state = client.call("gamestate")
    print(f"\n--- Test 4: After third pack cycle ({total_added} total) ---")
    xm = dump_hologram(state.get("jokers", {}).get("cards", []))
    expected = 1 + 0.25 * total_added
    print(f"  Expected xmult: {expected}")
    r = play_controlled_hand(client, pair_hand, state, label=f"After {total_added} cards (cycle 3)", hand_num=4)
    if r: results.append(r)

    # ── Test 5: DNA interaction — does mid-scoring card add break things? ──
    print(f"\n--- Test 5: Adding DNA Joker for mid-scoring increment test ---")
    try:
        client.call("add", {"key": "j_dna"})
    except APIError as e:
        print(f"  FAILED to add DNA: {e.message}")

    # Beat blind, go to next round
    give_money(client)
    state = advance_to_shop(client)
    state = advance_to_selecting_hand(client)
    state = client.call("gamestate")
    pre_dna_xm = dump_hologram(state.get("jokers", {}).get("cards", []))
    print(f"  Pre-DNA hand: Hologram at {pre_dna_xm}")
    r = play_controlled_hand(client, pair_hand, state,
        label=f"With DNA (pre={pre_dna_xm}, during +0.25?)", hand_num=5)
    if r: results.append(r)

    # Check what Hologram shows AFTER the DNA hand
    time.sleep(0.5)
    state = client.call("gamestate")
    if state.get("state") == "SELECTING_HAND":
        post_dna_xm = dump_hologram(state.get("jokers", {}).get("cards", []))
        print(f"  Post-DNA hand: Hologram at {post_dna_xm}")
        if pre_dna_xm and post_dna_xm:
            delta = post_dna_xm - pre_dna_xm
            print(f"  Delta: {delta:+.2f} (expected +0.25 from DNA copy)")

    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=12346)
    args = parser.parse_args()

    client = BalatroClient("localhost", args.port)
    client.timeout = 20

    results = run_hologram_tests(client)

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    matches = sum(1 for r in results if r["diff"] == 0)
    mismatches = sum(1 for r in results if r["diff"] != 0)
    print(f"  {matches} MATCH, {mismatches} MISMATCH out of {len(results)} tests\n")

    for r in results:
        status = "MATCH" if r["diff"] == 0 else "MISMATCH"
        holo_note = ""
        if r.get("bot_holo_xmult") is not None:
            holo_note = f" (holo_xm={r['bot_holo_xmult']})"
        print(f"  [{status:8s}] {r['label']:55s} est={r['est']:6d} actual={r['actual']:6d} diff={r['diff']:+d}{holo_note}")

    # Interpretation
    print()
    fresh_results = [r for r in results if r.get("bot_holo_xmult") and r["bot_holo_xmult"] <= 1.0]
    active_results = [r for r in results if r.get("bot_holo_xmult") and r["bot_holo_xmult"] > 1.0]
    dna_results = [r for r in results if "DNA" in r["label"]]

    if fresh_results:
        fm = sum(1 for r in fresh_results if r["diff"] == 0)
        print(f"  Fresh (x<=1.0): {fm}/{len(fresh_results)} match")
        if fm < len(fresh_results):
            print(f"    >>> Bug: even fresh Hologram mismatches!")

    if active_results:
        am = sum(1 for r in active_results if r["diff"] == 0)
        print(f"  Active (x>1.0): {am}/{len(active_results)} match")
        if am < len(active_results):
            print(f"    >>> Hologram scoring is off when x_mult > 1")

    if dna_results:
        dm = sum(1 for r in dna_results if r["diff"] == 0)
        print(f"  DNA hands:      {dm}/{len(dna_results)} match")
        if dm < len(dna_results):
            print(f"    >>> DNA mid-scoring increment causes stale snapshot")


if __name__ == "__main__":
    main()
