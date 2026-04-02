"""Integration tests for batch 053 scoring mismatches:
Flower Pot + WILD, Ride the Bus + Pareidolia, Steel Joker,
Bull Joker (money-dependent), and The Hook held-card recalc.

Usage:
    python test_scoring_053.py [--port PORT]

Requires a running balatrobot server.
"""

import argparse
import logging
import sys
import time

sys.path.insert(0, "src")

from balatrobot.cli.client import BalatroClient, APIError
from balatro_bot.hand_evaluator import score_hand_detailed, classify_hand, _scoring_cards_for
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


# ---------------------------------------------------------------------------
# Test: Flower Pot + WILD cards
# ---------------------------------------------------------------------------

def test_flower_pot_wild(client, results):
    """Flower Pot requires all 4 suits among scoring cards for x3 mult.
    WILD cards count as all 4 suits — verify that the game agrees."""

    # Test 1: Flower Pot with a WILD card providing the missing suit
    # 3 natural suits + 1 WILD = all 4 suits covered
    print(f"\n{'='*60}")
    print("TEST: Flower Pot + 1 WILD card providing 4th suit")
    print(f"{'='*60}")
    state = setup_game(client, "FPWILD1",
        joker_keys=["j_flower_pot"],
        card_configs=[
            {"key": "S_K"}, {"key": "H_K"},       # Spades, Hearts
            {"key": "D_3"},                         # Diamonds
            {"key": "C_4", "enhancement": "WILD"},  # WILD = all suits (covers Clubs)
            {"key": "S_5"},                         # pad
        ])
    hand_cards = state.get("hand", {}).get("cards", [])
    print(f"  Hand: {len(hand_cards)} cards")
    for i, c in enumerate(hand_cards): dump_card(i, c)
    play_indices = list(range(len(hand_cards) - 5, len(hand_cards)))
    results.append(play_and_compare(client, state, play_indices, "FlowerPot: 3 suits + WILD"))

    # Test 2: Flower Pot with 2 WILD cards (redundant coverage)
    print(f"\n{'='*60}")
    print("TEST: Flower Pot + 2 WILD cards (full coverage)")
    print(f"{'='*60}")
    state = setup_game(client, "FPWILD2",
        joker_keys=["j_flower_pot"],
        card_configs=[
            {"key": "S_K"}, {"key": "H_K"},
            {"key": "D_3", "enhancement": "WILD"},
            {"key": "C_4", "enhancement": "WILD"},
            {"key": "S_5"},
        ])
    hand_cards = state.get("hand", {}).get("cards", [])
    print(f"  Hand: {len(hand_cards)} cards")
    for i, c in enumerate(hand_cards): dump_card(i, c)
    play_indices = list(range(len(hand_cards) - 5, len(hand_cards)))
    results.append(play_and_compare(client, state, play_indices, "FlowerPot: 2 WILD cards"))

    # Test 3: Flower Pot WITHOUT full suit coverage — should NOT trigger
    print(f"\n{'='*60}")
    print("TEST: Flower Pot — only 2 suits (no trigger)")
    print(f"{'='*60}")
    state = setup_game(client, "FPWILD3",
        joker_keys=["j_flower_pot"],
        card_configs=[
            {"key": "S_K"}, {"key": "S_Q"},
            {"key": "H_3"}, {"key": "H_4"}, {"key": "S_5"},
        ])
    hand_cards = state.get("hand", {}).get("cards", [])
    print(f"  Hand: {len(hand_cards)} cards")
    for i, c in enumerate(hand_cards): dump_card(i, c)
    play_indices = list(range(len(hand_cards) - 5, len(hand_cards)))
    results.append(play_and_compare(client, state, play_indices, "FlowerPot: 2 suits (no trigger)"))

    # Test 4: Flower Pot with only WILD cards providing coverage (no natural diversity)
    print(f"\n{'='*60}")
    print("TEST: Flower Pot — all same suit + 1 WILD (WILD provides missing suits)")
    print(f"{'='*60}")
    state = setup_game(client, "FPWILD4",
        joker_keys=["j_flower_pot"],
        card_configs=[
            {"key": "S_K"}, {"key": "S_Q"},
            {"key": "S_3"}, {"key": "S_4"},
            {"key": "S_5", "enhancement": "WILD"},  # WILD on a Spade — adds H/D/C
        ])
    hand_cards = state.get("hand", {}).get("cards", [])
    print(f"  Hand: {len(hand_cards)} cards")
    for i, c in enumerate(hand_cards): dump_card(i, c)
    play_indices = list(range(len(hand_cards) - 5, len(hand_cards)))
    results.append(play_and_compare(client, state, play_indices, "FlowerPot: all Spades + 1 WILD"))


# ---------------------------------------------------------------------------
# Test: Ride the Bus + Pareidolia
# ---------------------------------------------------------------------------

def test_ride_the_bus_pareidolia(client, results):
    """Ride the Bus gains +mult per hand without face cards.
    Pareidolia makes ALL cards face — so Ride the Bus should always reset."""

    # Test 1: Ride the Bus alone with no face cards — should gain mult
    print(f"\n{'='*60}")
    print("TEST: Ride the Bus alone — no face cards (should gain mult)")
    print(f"{'='*60}")
    state = setup_game(client, "RTB1",
        joker_keys=["j_ride_the_bus"],
        card_configs=[
            {"key": "S_2"}, {"key": "H_3"},
            {"key": "D_4"}, {"key": "C_5"}, {"key": "S_7"},
        ])
    hand_cards = state.get("hand", {}).get("cards", [])
    jokers = state.get("jokers", {}).get("cards", [])
    print(f"  Hand: {len(hand_cards)} cards")
    for i, c in enumerate(hand_cards): dump_card(i, c)
    print(f"  Ride the Bus ability: {jokers[0].get('value', {}).get('ability', {})}")
    play_indices = list(range(len(hand_cards) - 5, len(hand_cards)))
    results.append(play_and_compare(client, state, play_indices, "RideTheBus: no face cards"))

    # Test 2: Ride the Bus alone WITH face cards — should reset
    print(f"\n{'='*60}")
    print("TEST: Ride the Bus alone — with face cards (should reset)")
    print(f"{'='*60}")
    state = setup_game(client, "RTB2",
        joker_keys=["j_ride_the_bus"],
        card_configs=[
            {"key": "S_K"}, {"key": "H_Q"},  # face cards
            {"key": "D_4"}, {"key": "C_5"}, {"key": "S_7"},
        ])
    hand_cards = state.get("hand", {}).get("cards", [])
    jokers = state.get("jokers", {}).get("cards", [])
    print(f"  Hand: {len(hand_cards)} cards")
    for i, c in enumerate(hand_cards): dump_card(i, c)
    print(f"  Ride the Bus ability: {jokers[0].get('value', {}).get('ability', {})}")
    play_indices = list(range(len(hand_cards) - 5, len(hand_cards)))
    results.append(play_and_compare(client, state, play_indices, "RideTheBus: with face cards"))

    # Test 3: Ride the Bus + Pareidolia — no natural face cards but all are "face"
    print(f"\n{'='*60}")
    print("TEST: Ride the Bus + Pareidolia — all cards are face (should reset)")
    print(f"{'='*60}")
    state = setup_game(client, "RTB3",
        joker_keys=["j_ride_the_bus", "j_pareidolia"],
        card_configs=[
            {"key": "S_2"}, {"key": "H_3"},
            {"key": "D_4"}, {"key": "C_5"}, {"key": "S_7"},
        ])
    hand_cards = state.get("hand", {}).get("cards", [])
    jokers = state.get("jokers", {}).get("cards", [])
    print(f"  Hand: {len(hand_cards)} cards")
    for i, c in enumerate(hand_cards): dump_card(i, c)
    for j in jokers:
        print(f"  {j.get('key')}: ability={j.get('value', {}).get('ability', {})}")
    play_indices = list(range(len(hand_cards) - 5, len(hand_cards)))
    results.append(play_and_compare(client, state, play_indices, "RideTheBus+Pareidolia: resets"))


# ---------------------------------------------------------------------------
# Test: Steel Joker scoring
# ---------------------------------------------------------------------------

def test_steel_joker(client, results):
    """Steel Joker: x0.2 mult per Steel card in full hand (held + played).
    Steel enhancement on scoring cards gives x1.5 mult per card (separate effect).
    Verify both interact correctly."""

    # Test 1: Steel Joker + 2 Steel cards in hand (held, not played)
    print(f"\n{'='*60}")
    print("TEST: Steel Joker + 2 Steel cards held (not played)")
    print(f"{'='*60}")
    state = setup_game(client, "STEEL1",
        joker_keys=["j_steel_joker"],
        card_configs=[
            {"key": "S_K"}, {"key": "H_K"},  # scoring pair (no Steel)
            {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"},  # pad
            {"key": "H_7", "enhancement": "STEEL"},  # held Steel
            {"key": "D_8", "enhancement": "STEEL"},  # held Steel
        ])
    hand_cards = state.get("hand", {}).get("cards", [])
    jokers = state.get("jokers", {}).get("cards", [])
    print(f"  Hand: {len(hand_cards)} cards")
    for i, c in enumerate(hand_cards): dump_card(i, c)
    for j in jokers:
        print(f"  {j.get('key')}: ability={j.get('value', {}).get('ability', {})}")
    # Play only the first 5 injected cards, hold the 2 Steel cards
    play_indices = list(range(len(hand_cards) - 7, len(hand_cards) - 2))
    results.append(play_and_compare(client, state, play_indices, "SteelJoker: 2 Steel held"))

    # Test 2: Steel Joker + Steel cards in scoring hand (played)
    print(f"\n{'='*60}")
    print("TEST: Steel Joker + 2 Steel cards played (scoring)")
    print(f"{'='*60}")
    state = setup_game(client, "STEEL2",
        joker_keys=["j_steel_joker"],
        card_configs=[
            {"key": "S_K", "enhancement": "STEEL"}, {"key": "H_K", "enhancement": "STEEL"},
            {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"},
        ])
    hand_cards = state.get("hand", {}).get("cards", [])
    jokers = state.get("jokers", {}).get("cards", [])
    print(f"  Hand: {len(hand_cards)} cards")
    for i, c in enumerate(hand_cards): dump_card(i, c)
    for j in jokers:
        print(f"  {j.get('key')}: ability={j.get('value', {}).get('ability', {})}")
    play_indices = list(range(len(hand_cards) - 5, len(hand_cards)))
    results.append(play_and_compare(client, state, play_indices, "SteelJoker: 2 Steel played"))

    # Test 3: Steel Joker + mix of held and played Steel
    print(f"\n{'='*60}")
    print("TEST: Steel Joker + 1 Steel played + 2 Steel held")
    print(f"{'='*60}")
    state = setup_game(client, "STEEL3",
        joker_keys=["j_steel_joker"],
        card_configs=[
            {"key": "S_K", "enhancement": "STEEL"}, {"key": "H_K"},  # 1 Steel scoring
            {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"},
            {"key": "H_7", "enhancement": "STEEL"},  # held Steel
            {"key": "D_8", "enhancement": "STEEL"},  # held Steel
        ])
    hand_cards = state.get("hand", {}).get("cards", [])
    jokers = state.get("jokers", {}).get("cards", [])
    print(f"  Hand: {len(hand_cards)} cards")
    for i, c in enumerate(hand_cards): dump_card(i, c)
    for j in jokers:
        print(f"  {j.get('key')}: ability={j.get('value', {}).get('ability', {})}")
    play_indices = list(range(len(hand_cards) - 7, len(hand_cards) - 2))
    results.append(play_and_compare(client, state, play_indices, "SteelJoker: 1 played + 2 held"))

    # Test 4: No Steel Joker, just Steel cards — baseline for Steel enhancement only
    print(f"\n{'='*60}")
    print("TEST: Steel cards WITHOUT Steel Joker (enhancement-only baseline)")
    print(f"{'='*60}")
    state = setup_game(client, "STEEL4",
        joker_keys=[],
        card_configs=[
            {"key": "S_K", "enhancement": "STEEL"}, {"key": "H_K", "enhancement": "STEEL"},
            {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"},
        ])
    hand_cards = state.get("hand", {}).get("cards", [])
    print(f"  Hand: {len(hand_cards)} cards")
    for i, c in enumerate(hand_cards): dump_card(i, c)
    play_indices = list(range(len(hand_cards) - 5, len(hand_cards)))
    results.append(play_and_compare(client, state, play_indices, "Steel cards only (no joker)"))


# ---------------------------------------------------------------------------
# Test: Bull Joker (money-dependent scoring)
# ---------------------------------------------------------------------------

def test_bull_joker(client, results):
    """Bull: +2 chips per dollar held. Money changes mid-round when cards with
    Gold seal or Gold enhancement score. Verify chips scale with money."""

    # Test 1: Bull Joker at game start money (~$4)
    print(f"\n{'='*60}")
    print("TEST: Bull Joker — starting money (~$4)")
    print(f"{'='*60}")
    state = setup_game(client, "BULL1",
        joker_keys=["j_bull"],
        card_configs=[
            {"key": "S_K"}, {"key": "H_K"},
            {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"},
        ])
    hand_cards = state.get("hand", {}).get("cards", [])
    jokers = state.get("jokers", {}).get("cards", [])
    money = state.get("money", 0)
    print(f"  Money: ${money}")
    print(f"  Hand: {len(hand_cards)} cards")
    for i, c in enumerate(hand_cards): dump_card(i, c)
    for j in jokers:
        print(f"  {j.get('key')}: ability={j.get('value', {}).get('ability', {})}")
    play_indices = list(range(len(hand_cards) - 5, len(hand_cards)))
    results.append(play_and_compare(client, state, play_indices, f"Bull: ${money} money"))

    # Test 2: Bull Joker with Gold cards (Gold enhancement = +$3 when scored)
    # Money increases during scoring — does Bull use pre- or post-gold money?
    print(f"\n{'='*60}")
    print("TEST: Bull Joker + Gold cards (money changes mid-scoring)")
    print(f"{'='*60}")
    state = setup_game(client, "BULL2",
        joker_keys=["j_bull"],
        card_configs=[
            {"key": "S_K", "enhancement": "GOLD"}, {"key": "H_K", "enhancement": "GOLD"},
            {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"},
        ])
    hand_cards = state.get("hand", {}).get("cards", [])
    jokers = state.get("jokers", {}).get("cards", [])
    money = state.get("money", 0)
    print(f"  Money: ${money}")
    print(f"  Hand: {len(hand_cards)} cards")
    for i, c in enumerate(hand_cards): dump_card(i, c)
    for j in jokers:
        print(f"  {j.get('key')}: ability={j.get('value', {}).get('ability', {})}")
    play_indices = list(range(len(hand_cards) - 5, len(hand_cards)))
    results.append(play_and_compare(client, state, play_indices, f"Bull+Gold: ${money} start"))

    # Test 3: Bull Joker with Gold Seal cards (+$3 when played)
    print(f"\n{'='*60}")
    print("TEST: Bull Joker + Gold Seal cards (seal gives money)")
    print(f"{'='*60}")
    state = setup_game(client, "BULL3",
        joker_keys=["j_bull"],
        card_configs=[
            {"key": "S_K", "seal": "GOLD"}, {"key": "H_K", "seal": "GOLD"},
            {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"},
        ])
    hand_cards = state.get("hand", {}).get("cards", [])
    jokers = state.get("jokers", {}).get("cards", [])
    money = state.get("money", 0)
    print(f"  Money: ${money}")
    print(f"  Hand: {len(hand_cards)} cards")
    for i, c in enumerate(hand_cards): dump_card(i, c)
    for j in jokers:
        print(f"  {j.get('key')}: ability={j.get('value', {}).get('ability', {})}")
    play_indices = list(range(len(hand_cards) - 5, len(hand_cards)))
    results.append(play_and_compare(client, state, play_indices, f"Bull+GoldSeal: ${money} start"))

    # Test 4: Bull Joker with To the Moon (extra interest) — just different money
    print(f"\n{'='*60}")
    print("TEST: Bull Joker + To Do List (bonus money from hand type)")
    print(f"{'='*60}")
    state = setup_game(client, "BULL4",
        joker_keys=["j_bull", "j_todo_list"],
        card_configs=[
            {"key": "S_K"}, {"key": "H_K"},
            {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"},
        ])
    hand_cards = state.get("hand", {}).get("cards", [])
    jokers = state.get("jokers", {}).get("cards", [])
    money = state.get("money", 0)
    print(f"  Money: ${money}")
    print(f"  Hand: {len(hand_cards)} cards")
    for i, c in enumerate(hand_cards): dump_card(i, c)
    for j in jokers:
        ab = j.get("value", {}).get("ability", {})
        print(f"  {j.get('key')}: ability={ab}")
    play_indices = list(range(len(hand_cards) - 5, len(hand_cards)))
    results.append(play_and_compare(client, state, play_indices, f"Bull+ToDoList: ${money} start"))


# ---------------------------------------------------------------------------
# Test: Glass card scoring (x2 always, ignore break chance)
# ---------------------------------------------------------------------------

def test_glass_scoring(client, results):
    """Glass cards: x2 mult per Glass card scored. The 1/4 break chance
    doesn't affect scoring — card always gives x2 even if it shatters after."""

    # Test 1: Single Glass card in scoring hand
    print(f"\n{'='*60}")
    print("TEST: Glass card scoring — 1 Glass card in pair")
    print(f"{'='*60}")
    state = setup_game(client, "GLASS1",
        joker_keys=[],
        card_configs=[
            {"key": "S_K", "enhancement": "GLASS"}, {"key": "H_K"},
            {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"},
        ])
    hand_cards = state.get("hand", {}).get("cards", [])
    print(f"  Hand: {len(hand_cards)} cards")
    for i, c in enumerate(hand_cards): dump_card(i, c)
    play_indices = list(range(len(hand_cards) - 5, len(hand_cards)))
    results.append(play_and_compare(client, state, play_indices, "Glass: 1 Glass in pair"))

    # Test 2: Two Glass cards
    print(f"\n{'='*60}")
    print("TEST: Glass card scoring — 2 Glass cards in pair (x2 x2 = x4)")
    print(f"{'='*60}")
    state = setup_game(client, "GLASS2",
        joker_keys=[],
        card_configs=[
            {"key": "S_K", "enhancement": "GLASS"}, {"key": "H_K", "enhancement": "GLASS"},
            {"key": "D_3"}, {"key": "C_4"}, {"key": "S_5"},
        ])
    hand_cards = state.get("hand", {}).get("cards", [])
    print(f"  Hand: {len(hand_cards)} cards")
    for i, c in enumerate(hand_cards): dump_card(i, c)
    play_indices = list(range(len(hand_cards) - 5, len(hand_cards)))
    results.append(play_and_compare(client, state, play_indices, "Glass: 2 Glass in pair (x4)"))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Test batch 053 scoring mismatches")
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

    test_flower_pot_wild(client, results)
    test_ride_the_bus_pareidolia(client, results)
    test_steel_joker(client, results)
    test_bull_joker(client, results)
    test_glass_scoring(client, results)

    print(f"\n\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    matches = 0
    mismatches = 0
    for r in results:
        if r is None:
            continue
        status = "MATCH" if r["diff"] == 0 else f"MISMATCH({r['diff']:+d})"
        if r["diff"] == 0:
            matches += 1
        else:
            mismatches += 1
        print(f"  {r['label']:45s} est={r['est']:>8d} actual={r['actual']:>8d} {status}")
    print(f"\n  Total: {matches + mismatches} tests, {matches} matches, {mismatches} mismatches")


if __name__ == "__main__":
    main()
