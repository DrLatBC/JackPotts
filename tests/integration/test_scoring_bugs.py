"""Test scoring mismatches: Smiley/Scary Face counting, Baseball Card rarity,
Gros Michel parsing, Square/Runner pre-increment, and Splash+Pareidolia.

Usage:
    python test_scoring_bugs.py [--port PORT]

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
    mod = _modifier(c)
    rank = c.get("value", {}).get("rank", "?")
    suit = c.get("value", {}).get("suit", "?")
    enh = mod.get("enhancement", "-")
    ed = mod.get("edition", "-")
    print(f"  [{i}] {rank}{suit} enh={enh} ed={ed} | chips={card_chip_value(c)} mult={card_mult_value(c)} | raw_mod={mod}")


def setup_game(client, seed, joker_keys=None, card_configs=None):
    """Start fresh game, sell existing jokers, discard hand, inject jokers + cards."""
    try:
        client.call("menu")
    except APIError:
        pass
    time.sleep(0.5)

    state = client.call("start", {"deck": "RED", "stake": "WHITE", "seed": seed})
    state = wait_for_state(client, ["SELECTING_HAND"])

    # Sell all existing jokers
    for i in range(state.get("jokers", {}).get("count", 0)):
        try:
            client.call("sell", {"joker": 0})
        except APIError:
            pass

    # Discard hand twice to clear
    for _ in range(2):
        state = client.call("gamestate")
        hand_cards = state.get("hand", {}).get("cards", [])
        if hand_cards:
            try:
                client.call("discard", {"cards": list(range(min(len(hand_cards), 5)))})
                time.sleep(0.2)
            except APIError:
                pass

    # Inject jokers
    if joker_keys:
        for jk in joker_keys:
            params = {"key": jk} if isinstance(jk, str) else jk
            try:
                client.call("add", params)
                print(f"  Added joker: {params.get('key')}")
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


def play_and_compare(client, state, play_indices, label=""):
    """Play cards at given indices, compare bot estimate vs actual."""
    hand_cards = state.get("hand", {}).get("cards", [])
    jokers = state.get("jokers", {}).get("cards", [])
    hand_levels = state.get("hands", {})

    played = [hand_cards[i] for i in play_indices]
    held = [c for j, c in enumerate(hand_cards) if j not in set(play_indices)]

    hand_name = classify_hand(played)
    scoring = _scoring_cards_for(hand_name, played)

    # Check for Splash — all played cards score
    joker_keys = {j.get("key") for j in jokers}
    if "j_splash" in joker_keys:
        scoring = played

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

    print(f"\n  Playing {hand_name}: indices={play_indices}")
    print(f"  Scoring cards: {len(scoring)} | Played cards: {len(played)} | Held cards: {len(held)}")
    print(f"  Jokers: {[j.get('key') for j in jokers]}")
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
        return {"label": label, "est": detail["total"], "actual": 0, "diff": 0}

    post_chips = new_state.get("round", {}).get("chips", 0)
    actual = post_chips - pre_chips
    diff = actual - detail["total"]

    print(f"  Actual score: {actual}")
    print(f"  Difference: {diff:+d}")

    status = "MATCH" if diff == 0 else f"MISMATCH({diff:+d})"
    if actual > 0 and diff != 0:
        print(f"  >> {status} (est/actual = {detail['total']/actual:.3f})")
    elif diff == 0:
        print(f"  >> MATCH")

    return {"label": label, "est": detail["total"], "actual": actual, "diff": diff}


def test_smiley_face(client, results):
    """Test Smiley Face: does it fire on non-scoring played cards? Held cards?"""

    # Test 1: Smiley with face cards scoring only (no Pareidolia)
    print(f"\n{'='*60}")
    print("TEST: Smiley Face — 2 face cards scoring (Pair of Kings)")
    print(f"{'='*60}")
    state = setup_game(client, "SMILEY1",
        joker_keys=["j_smiley"],
        card_configs=[
            {"key": "S_K"}, {"key": "H_K"},  # scoring pair
            {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"},  # non-face pad
        ])
    hand_cards = state.get("hand", {}).get("cards", [])
    print(f"  Hand: {len(hand_cards)} cards")
    for i, c in enumerate(hand_cards): dump_card(i, c)
    n = 5
    play_indices = list(range(len(hand_cards) - n, len(hand_cards)))
    results.append(play_and_compare(client, state, play_indices, "Smiley: 2 face scoring"))

    # Test 2: Smiley + Pareidolia (all cards are face)
    print(f"\n{'='*60}")
    print("TEST: Smiley + Pareidolia — all 5 played cards are 'face'")
    print(f"{'='*60}")
    state = setup_game(client, "SMILEY2",
        joker_keys=["j_smiley", "j_pareidolia"],
        card_configs=[
            {"key": "S_K"}, {"key": "H_K"},  # scoring pair
            {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"},  # non-scoring but face via Pareidolia
        ])
    hand_cards = state.get("hand", {}).get("cards", [])
    print(f"  Hand: {len(hand_cards)} cards")
    for i, c in enumerate(hand_cards): dump_card(i, c)
    play_indices = list(range(len(hand_cards) - 5, len(hand_cards)))
    results.append(play_and_compare(client, state, play_indices, "Smiley+Pareidolia: 5 played"))

    # Test 3: Smiley + Pareidolia, only 2 cards played (no non-scoring cards)
    print(f"\n{'='*60}")
    print("TEST: Smiley + Pareidolia — 2 cards played (Pair, no pad)")
    print(f"{'='*60}")
    state = setup_game(client, "SMILEY3",
        joker_keys=["j_smiley", "j_pareidolia"],
        card_configs=[
            {"key": "S_K"}, {"key": "H_K"},
        ])
    hand_cards = state.get("hand", {}).get("cards", [])
    print(f"  Hand: {len(hand_cards)} cards")
    for i, c in enumerate(hand_cards): dump_card(i, c)
    # Play only the 2 injected Kings
    play_indices = [len(hand_cards) - 2, len(hand_cards) - 1]
    results.append(play_and_compare(client, state, play_indices, "Smiley+Pareidolia: 2 played"))

    # Test 4: Scary Face (chips version) with same setup
    print(f"\n{'='*60}")
    print("TEST: Scary Face + Pareidolia — 5 played cards")
    print(f"{'='*60}")
    state = setup_game(client, "SCARY1",
        joker_keys=["j_scary_face", "j_pareidolia"],
        card_configs=[
            {"key": "S_K"}, {"key": "H_K"},
            {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"},
        ])
    hand_cards = state.get("hand", {}).get("cards", [])
    print(f"  Hand: {len(hand_cards)} cards")
    for i, c in enumerate(hand_cards): dump_card(i, c)
    play_indices = list(range(len(hand_cards) - 5, len(hand_cards)))
    results.append(play_and_compare(client, state, play_indices, "Scary+Pareidolia: 5 played"))


def test_baseball_card(client, results):
    """Test Baseball Card: x1.5 per Uncommon joker. Check rarity detection."""

    # Add Baseball + known Uncommon jokers and see if count matches
    print(f"\n{'='*60}")
    print("TEST: Baseball Card — 2 Uncommon jokers (Fibonacci + Scholar)")
    print(f"{'='*60}")
    # Fibonacci (Uncommon) and Scholar (Common) — only Fibonacci should count
    state = setup_game(client, "BASEBALL1",
        joker_keys=["j_baseball", "j_fibonacci", "j_scholar"],
        card_configs=[
            {"key": "S_A"}, {"key": "H_A"},
            {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"},
        ])
    hand_cards = state.get("hand", {}).get("cards", [])
    jokers = state.get("jokers", {}).get("cards", [])
    print(f"  Joker rarity data:")
    for j in jokers:
        rarity = j.get("value", {}).get("rarity")
        print(f"    {j.get('key')}: rarity={rarity} label={j.get('label')}")
    for i, c in enumerate(hand_cards): dump_card(i, c)
    play_indices = list(range(len(hand_cards) - 5, len(hand_cards)))
    results.append(play_and_compare(client, state, play_indices, "Baseball: Fib(Unc)+Scholar(Com)"))

    # Add more Uncommon jokers to amplify the effect
    print(f"\n{'='*60}")
    print("TEST: Baseball Card — 3 Uncommon jokers")
    print(f"{'='*60}")
    state = setup_game(client, "BASEBALL2",
        joker_keys=["j_baseball", "j_fibonacci", "j_even_steven", "j_odd_todd"],
        card_configs=[
            {"key": "S_A"}, {"key": "H_A"},
            {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"},
        ])
    jokers = state.get("jokers", {}).get("cards", [])
    print(f"  Joker rarity data:")
    for j in jokers:
        rarity = j.get("value", {}).get("rarity")
        print(f"    {j.get('key')}: rarity={rarity} label={j.get('label')}")
    hand_cards = state.get("hand", {}).get("cards", [])
    for i, c in enumerate(hand_cards): dump_card(i, c)
    play_indices = list(range(len(hand_cards) - 5, len(hand_cards)))
    results.append(play_and_compare(client, state, play_indices, "Baseball: 3 Uncommon"))


def test_gros_michel(client, results):
    """Test Gros Michel: xMult parsing from effect text."""

    print(f"\n{'='*60}")
    print("TEST: Gros Michel — x15 Mult (base value)")
    print(f"{'='*60}")
    state = setup_game(client, "GROS1",
        joker_keys=["j_gros_michel"],
        card_configs=[
            {"key": "S_A"}, {"key": "H_A"},
            {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"},
        ])
    jokers = state.get("jokers", {}).get("cards", [])
    for j in jokers:
        ab = j.get("value", {}).get("ability", {})
        effect = j.get("value", {}).get("effect", "")
        print(f"  {j.get('key')}: ability={ab} effect={effect[:100]}")
    hand_cards = state.get("hand", {}).get("cards", [])
    for i, c in enumerate(hand_cards): dump_card(i, c)
    play_indices = list(range(len(hand_cards) - 5, len(hand_cards)))
    results.append(play_and_compare(client, state, play_indices, "Gros Michel: base"))

    # Test with Acrobat to check interaction
    print(f"\n{'='*60}")
    print("TEST: Gros Michel + Acrobat — last hand x3")
    print(f"{'='*60}")
    state = setup_game(client, "GROS2",
        joker_keys=["j_gros_michel", "j_acrobat"],
        card_configs=[
            {"key": "S_A"}, {"key": "H_A"},
            {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"},
        ])
    jokers = state.get("jokers", {}).get("cards", [])
    for j in jokers:
        ab = j.get("value", {}).get("ability", {})
        effect = j.get("value", {}).get("effect", "")
        print(f"  {j.get('key')}: ability={ab} effect={effect[:100]}")

    # Burn hands to get to last hand
    hand_cards = state.get("hand", {}).get("cards", [])
    hands_left = state.get("round", {}).get("hands_left", 4)
    print(f"  hands_left={hands_left}, burning {hands_left - 1} hands to reach last hand...")
    for _ in range(hands_left - 1):
        state = client.call("gamestate")
        hc = state.get("hand", {}).get("cards", [])
        if hc:
            try:
                client.call("play", {"cards": [0]})
                time.sleep(0.2)
            except APIError:
                break

    state = client.call("gamestate")
    hand_cards = state.get("hand", {}).get("cards", [])
    hands_left = state.get("round", {}).get("hands_left", 0)
    print(f"  Now hands_left={hands_left}")
    for i, c in enumerate(hand_cards): dump_card(i, c)
    play_indices = list(range(min(5, len(hand_cards))))
    results.append(play_and_compare(client, state, play_indices, "Gros Michel+Acrobat: last hand"))


def test_square_pre_increment(client, results):
    """Test Square Joker: does the game increment +4 before scoring on 4-card plays?"""

    print(f"\n{'='*60}")
    print("TEST: Square Joker — 4-card play (should increment before scoring)")
    print(f"{'='*60}")
    state = setup_game(client, "SQUARE1",
        joker_keys=["j_square"],
        card_configs=[
            {"key": "S_K"}, {"key": "H_K"},
            {"key": "D_3"}, {"key": "C_4"},  # exactly 4 cards
        ])
    jokers = state.get("jokers", {}).get("cards", [])
    for j in jokers:
        ab = j.get("value", {}).get("ability", {})
        effect = j.get("value", {}).get("effect", "")
        print(f"  {j.get('key')}: ability={ab} effect={effect[:100]}")
    hand_cards = state.get("hand", {}).get("cards", [])
    for i, c in enumerate(hand_cards): dump_card(i, c)
    play_indices = list(range(len(hand_cards) - 4, len(hand_cards)))
    results.append(play_and_compare(client, state, play_indices, "Square: 4-card play"))

    # Test 2: 5-card play — should NOT increment
    print(f"\n{'='*60}")
    print("TEST: Square Joker — 5-card play (should NOT increment)")
    print(f"{'='*60}")
    state = setup_game(client, "SQUARE2",
        joker_keys=["j_square"],
        card_configs=[
            {"key": "S_K"}, {"key": "H_K"},
            {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"},
        ])
    jokers = state.get("jokers", {}).get("cards", [])
    for j in jokers:
        ab = j.get("value", {}).get("ability", {})
        effect = j.get("value", {}).get("effect", "")
        print(f"  {j.get('key')}: ability={ab} effect={effect[:100]}")
    hand_cards = state.get("hand", {}).get("cards", [])
    for i, c in enumerate(hand_cards): dump_card(i, c)
    play_indices = list(range(len(hand_cards) - 5, len(hand_cards)))
    results.append(play_and_compare(client, state, play_indices, "Square: 5-card play"))


def test_splash_pareidolia(client, results):
    """Test Splash + Pareidolia: does the combo add extra mult?"""

    print(f"\n{'='*60}")
    print("TEST: Splash + Pareidolia — all cards score, all are face")
    print(f"{'='*60}")
    state = setup_game(client, "SPLASH1",
        joker_keys=["j_splash", "j_pareidolia"],
        card_configs=[
            {"key": "S_K"}, {"key": "H_K"},
            {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"},
        ])
    hand_cards = state.get("hand", {}).get("cards", [])
    for i, c in enumerate(hand_cards): dump_card(i, c)
    play_indices = list(range(len(hand_cards) - 5, len(hand_cards)))
    results.append(play_and_compare(client, state, play_indices, "Splash+Pareidolia: 5 cards"))

    # Splash only (no Pareidolia) — baseline
    print(f"\n{'='*60}")
    print("TEST: Splash only — all cards score, no face bonus")
    print(f"{'='*60}")
    state = setup_game(client, "SPLASH2",
        joker_keys=["j_splash"],
        card_configs=[
            {"key": "S_K"}, {"key": "H_K"},
            {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"},
        ])
    hand_cards = state.get("hand", {}).get("cards", [])
    for i, c in enumerate(hand_cards): dump_card(i, c)
    play_indices = list(range(len(hand_cards) - 5, len(hand_cards)))
    results.append(play_and_compare(client, state, play_indices, "Splash only: 5 cards"))


def test_joker_editions(client, results):
    """Test joker editions: HOLO (+10 mult), FOIL (+50 chips), POLYCHROME (x1.5)."""

    # HOLO joker — should add +10 mult
    print(f"\n{'='*60}")
    print("TEST: HOLO edition on Jolly Joker (+8 mult on Pair + 10 mult from HOLO)")
    print(f"{'='*60}")
    state = setup_game(client, "JKED1",
        joker_keys=[{"key": "j_jolly", "edition": "HOLO"}],
        card_configs=[
            {"key": "S_K"}, {"key": "H_K"},
            {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"},
        ])
    jokers = state.get("jokers", {}).get("cards", [])
    for j in jokers:
        mod = j.get("modifier", {})
        if not isinstance(mod, dict): mod = {}
        ab = j.get("value", {}).get("ability", {})
        print(f"  {j.get('key')}: edition={mod.get('edition', '-')} edition_mult={mod.get('edition_mult')} ability={ab}")
    hand_cards = state.get("hand", {}).get("cards", [])
    for i, c in enumerate(hand_cards): dump_card(i, c)
    play_indices = list(range(len(hand_cards) - 5, len(hand_cards)))
    results.append(play_and_compare(client, state, play_indices, "Jolly Joker (HOLO edition)"))

    # FOIL joker — should add +50 chips
    print(f"\n{'='*60}")
    print("TEST: FOIL edition on Jolly Joker (+8 mult on Pair + 50 chips from FOIL)")
    print(f"{'='*60}")
    state = setup_game(client, "JKED2",
        joker_keys=[{"key": "j_jolly", "edition": "FOIL"}],
        card_configs=[
            {"key": "S_K"}, {"key": "H_K"},
            {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"},
        ])
    jokers = state.get("jokers", {}).get("cards", [])
    for j in jokers:
        mod = j.get("modifier", {})
        if not isinstance(mod, dict): mod = {}
        print(f"  {j.get('key')}: edition={mod.get('edition', '-')} edition_chips={mod.get('edition_chips')} ")
    hand_cards = state.get("hand", {}).get("cards", [])
    for i, c in enumerate(hand_cards): dump_card(i, c)
    play_indices = list(range(len(hand_cards) - 5, len(hand_cards)))
    results.append(play_and_compare(client, state, play_indices, "Jolly Joker (FOIL edition)"))

    # POLYCHROME joker — should apply x1.5
    print(f"\n{'='*60}")
    print("TEST: POLYCHROME edition on Jolly Joker (+8 mult on Pair * x1.5 from POLY)")
    print(f"{'='*60}")
    state = setup_game(client, "JKED3",
        joker_keys=[{"key": "j_jolly", "edition": "POLYCHROME"}],
        card_configs=[
            {"key": "S_K"}, {"key": "H_K"},
            {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"},
        ])
    jokers = state.get("jokers", {}).get("cards", [])
    for j in jokers:
        mod = j.get("modifier", {})
        if not isinstance(mod, dict): mod = {}
        print(f"  {j.get('key')}: edition={mod.get('edition', '-')} edition_x_mult={mod.get('edition_x_mult')}")
    hand_cards = state.get("hand", {}).get("cards", [])
    for i, c in enumerate(hand_cards): dump_card(i, c)
    play_indices = list(range(len(hand_cards) - 5, len(hand_cards)))
    results.append(play_and_compare(client, state, play_indices, "Jolly Joker (POLYCHROME edition)"))

    # Plain joker (no edition) — baseline
    print(f"\n{'='*60}")
    print("TEST: Plain Jolly Joker (no edition) — baseline")
    print(f"{'='*60}")
    state = setup_game(client, "JKED4",
        joker_keys=["j_jolly"],
        card_configs=[
            {"key": "S_K"}, {"key": "H_K"},
            {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"},
        ])
    hand_cards = state.get("hand", {}).get("cards", [])
    for i, c in enumerate(hand_cards): dump_card(i, c)
    play_indices = list(range(len(hand_cards) - 5, len(hand_cards)))
    results.append(play_and_compare(client, state, play_indices, "Jolly Joker (no edition)"))


def main():
    parser = argparse.ArgumentParser(description="Test scoring bug hypotheses")
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

    test_smiley_face(client, results)
    test_baseball_card(client, results)
    test_gros_michel(client, results)
    test_square_pre_increment(client, results)
    test_splash_pareidolia(client, results)
    test_joker_editions(client, results)

    print(f"\n\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    for r in results:
        if r is None:
            continue
        status = "MATCH" if r["diff"] == 0 else f"MISMATCH({r['diff']:+d})"
        print(f"  {r['label']:45s} est={r['est']:>8d} actual={r['actual']:>8d} {status}")


if __name__ == "__main__":
    main()
