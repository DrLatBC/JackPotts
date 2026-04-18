# Testing

Two tiers: unit tests (fast, no game required) and integration tests (require a live Balatro instance).

## Unit tests

```bash
pytest                                   # all
pytest tests/test_scoring_accuracy.py    # one file
pytest -k madness                        # by keyword
```

380 tests covering scoring, joker effects, shop evaluation, discard policy, state adaptation, strategy derivation, and context computation.

## Integration tests

Run directly with `python`, not pytest. All support `--start-server` to auto-launch a headless Balatro instance:

```bash
python tests/integration/test_force_boss.py --start-server
python tests/integration/test_boss.py "The Hook" --start-server
python tests/integration/test_hook_scaling.py --bucket A2 --start-server
```

Or attach to an already-running server with `--port 12346`.

## Harness (`tests/integration/harness.py`)

| Function | Description |
|----------|-------------|
| `start_server(port)` | Launches a headless balatrobot server and waits for health |
| `ensure_server(port)` | Connects to existing server or starts a new one |
| `setup_game(client, seed)` | Starts a fresh game, waits for SELECTING_HAND |
| `setup_game_full(client, seed, joker_keys, card_configs)` | Full setup — sells defaults, discards hand, injects jokers and cards |
| `advance_to_boss_select(client, target_ante)` | Fast-forwards to boss select at a specific ante |
| `force_boss(client, boss_name)` | Forces a specific boss blind (e.g. `"The Ox"`, `"The Flint"`) |
| `beat_blind_fast(client, state)` | Sets chips to 999999 and plays to win the current blind |
| `cheat_win_if_needed(client, blind_name)` | Beats the named blind only if still playing it |
| `inject_jokers(client, joker_keys)` | Sells existing jokers and adds the specified ones |
| `inject_god_mode(client)` | Injects an overpowered build for win testing |
| `burn_discards(client, target)` | Burns N discards to advance discard-tracking jokers |
| `wait_for_state(client, targets)` | Polls until a target state, auto-advancing through intermediates |
| `take_screenshot(client, label)` | Captures a screenshot for debugging |

## Card format

`{Suit}_{Rank}` — `H_K` = King of Hearts, `S_A` = Ace of Spades, `D_T` = 10 of Diamonds, `C_2` = 2 of Clubs.

## Writing a test

```python
from harness import ensure_server, stop_server, setup_game_full, force_boss, advance_to_boss_select

def test_example():
    client, server = ensure_server()
    try:
        setup_game_full(
            client, seed="TESTSEED",
            joker_keys=["j_four_fingers", "j_shortcut"],
            card_configs=[
                {"key": "H_K"},                     # King of Hearts
                {"key": "S_A", "edition": "FOIL"},  # Foil Ace of Spades
            ],
        )

        advance_to_boss_select(client, target_ante=2)
        force_boss(client, "The Flint")

        state = client.call("gamestate")
        predicted = your_scoring_function(state)
        result = client.call("play", {"cards": [0, 1, 2, 3, 4]})
        actual = result["chips"]
        assert predicted == actual, f"Mismatch: {predicted} vs {actual}"
    finally:
        if server:
            stop_server(server)
```

## Available suites

| File | Tests |
|------|-------|
| `test_batch069_fixes.py` | Four Fingers, Shortcut, Flower Pot, Ox, Luchador edges |
| `test_shortcut_edges.py` | Shortcut gapped straights, combined with Four Fingers |
| `test_ox.py` | The Ox — most-played hand locking and money zeroing |
| `test_boss.py` | General boss blind scoring |
| `test_hook_scaling.py` | Scaling/state-dependent joker scoring under bosses |
| `test_win.py` | Full game win path (seed `GODMODE1`) |
| `test_glass_poly.py` | Glass + Polychrome interactions |
| `test_holo_edition.py` | Holographic edition scoring |
| `test_percard_scoring.py` | Per-card scoring breakdown validation |
| `test_rearrange.py` | Card rearrangement visual + logical ordering |
