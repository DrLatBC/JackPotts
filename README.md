# Jack Potts

[![built with Python](https://img.shields.io/badge/built%20with-Python-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![license MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![version v0.1.0](https://img.shields.io/badge/version-v0.2.0--beta-blue)](https://github.com/DrLatBC/JackPotts/releases)
[![tests 233 passing](https://img.shields.io/badge/tests-233%20passing-brightgreen)](tests/)
[![platform Windows](https://img.shields.io/badge/platform-Windows-lightgrey)](https://github.com/DrLatBC/JackPotts)
[![PRs welcome](https://img.shields.io/badge/PRs-welcome-brightgreen)](https://github.com/DrLatBC/JackPotts/pulls)

Rule-based bot that plays [Balatro](https://store.steampowered.com/app/2379780/Balatro/) autonomously via the [balatrobot](https://github.com/coder/balatrobot) mod's JSON-RPC API. It evaluates every game state through a priority-ordered rule engine, picking the first rule that fires — no ML, no tree search, just hand-tuned heuristics.

The bot fully simulates Balatro's scoring pipeline — joker effects, card enhancements, editions, seals, retriggers, and boss blind modifiers — to predict exact chip totals for every candidate hand before choosing what to play.

**Current peak: Ante 11 | Scoring accuracy: 99.5% | 2.5% win rate**

## Prerequisites

- **Balatro** (Steam)
- **[Lovely](https://github.com/ethangreen-dev/lovely-injector)** mod loader for Balatro
- **[balatrobot mod](https://github.com/DrLatBC/balatrobot)** (our fork) installed into Balatro — provides the JSON-RPC server the bot talks to
- **Python 3.11+**

> **Why our fork?** Jack Potts requires API fields and fixes not yet in [upstream balatrobot](https://github.com/coder/balatrobot): edition scoring values, joker ability data, The Ox's locked hand, Ancient Joker suit, boss blind forcing, endless mode support, and several endpoint hang fixes. A [PR is open](https://github.com/coder/balatrobot/pull/181) to merge these upstream — once accepted, the official mod will work. Until then, use our fork.

## Installation

```bash
git clone <repo-url>
cd balatro
pip install -e ".[dev]"
```

This installs the bot as an editable package along with `httpx`, `balatrobot` (client library), and `pytest` for development.

## Usage

### Start the balatrobot server

Launch a headless Balatro instance with the mod's RPC server:

```bash
uvx balatrobot serve --port 12346
```

### Run a single game

```bash
balatro-bot --port 12346 --start
```

Key flags:

| Flag | Default | Description |
|------|---------|-------------|
| `--port` | 12346 | RPC server port |
| `--start` | off | Start a new game automatically |
| `--deck` | RED | Deck to use |
| `--stake` | WHITE | Stake level |
| `--seed` | random | Force a specific seed |
| `--games` | 1 | Number of games to play back-to-back |
| `--verbose` | off | Debug-level logging |

### Run multiple instances in parallel

The supervisor manages N bot+server pairs with health monitoring and auto-restart:

```bash
python -m balatro_bot.supervisor --instances 4
```

Logs go to `bot_log/<port>/` with per-game and per-scoring breakdowns.

### Stats

Analyze completed runs:

```bash
python -m stats                  # latest batch
python -m stats --batch 55       # specific batch
```

## Project Structure

```
src/balatro_bot/
    bot.py              Main game loop — polls state, dispatches actions
    engine.py           Priority-ordered rule engine, first match fires
    context.py          Per-tick cached context (best hand, chips, strategy)
    strategy.py         Derives hand/suit/rank affinity from owned jokers
    rules/              Rule implementations per game state
    domain/
        models/         Typed game state snapshots
        scoring/        Hand classification, chip/mult calc, draw probability
        policy/         Play, discard, shop, consumable, and pack policies
    joker_effects/      Joker scoring pipeline: chips -> mult -> xmult
    infrastructure/     API dict <-> typed model adapters

tests/                  Pytest unit tests (233 tests)
tests/integration/      Integration tests requiring a running balatrobot server
stats/                  Game replay analysis and reporting
docs/                   Architecture docs and refactor plans
```

## Running Tests

Unit tests (no server required):

```bash
pytest
```

### Integration Test Harness

The `tests/integration/` directory contains a full in-game test harness that launches Balatro, injects specific game states, plays hands, and compares the bot's predicted score against the game's actual score. This is how we verify scoring accuracy at 99.5%.

**Quick start** — run any test with `--start-server` to auto-launch a headless Balatro instance:

```bash
python tests/integration/test_batch069_fixes.py --start-server
```

Or connect to an already-running server:

```bash
python tests/integration/test_batch069_fixes.py --port 12346
```

#### Harness Helpers (`harness.py`)

All integration tests share a common harness with these key functions:

| Function | Description |
|----------|-------------|
| `start_server(port)` | Launches a headless balatrobot server and waits for health |
| `ensure_server(port)` | Connects to existing server or starts a new one |
| `setup_game(client, seed)` | Starts a fresh game with a fixed seed, waits for SELECTING_HAND |
| `setup_game_full(client, seed, joker_keys, card_configs)` | Full setup — sells default jokers, discards hand, injects specific jokers and cards |
| `advance_to_boss_select(client, target_ante)` | Fast-forwards through blinds to reach boss select at a specific ante |
| `force_boss(client, boss_name)` | Forces a specific boss blind via the `set` API (e.g. `"The Ox"`, `"The Flint"`) |
| `beat_blind_fast(client, state)` | Sets chips to 999999 and plays to instantly win the current blind |
| `cheat_win_if_needed(client, blind_name)` | Beats the named blind only if still playing it |
| `inject_jokers(client, joker_keys)` | Sells existing jokers and adds the specified ones |
| `inject_god_mode(client)` | Injects an overpowered build (leveled High Card + power jokers) for win testing |
| `burn_discards(client, target)` | Burns N discards to advance discard-tracking jokers (Yorick, etc.) |
| `wait_for_state(client, targets)` | Polls until the game reaches a target state, auto-advancing through intermediate states |
| `take_screenshot(client, label)` | Captures a screenshot for debugging failed tests |

#### Writing a Test

A typical integration test follows this pattern:

```python
from harness import ensure_server, stop_server, setup_game_full, force_boss, advance_to_boss_select

def test_example():
    client, server = ensure_server()
    try:
        # Set up a specific scenario
        setup_game_full(client, seed="TESTSEED",
                        joker_keys=["j_four_fingers", "j_shortcut"],
                        card_configs=[
                            {"key": "H_K"},  # King of Hearts
                            {"key": "S_A", "edition": "FOIL"},  # Foil Ace of Spades
                        ])

        # Optionally force a boss blind
        advance_to_boss_select(client, target_ante=2)
        force_boss(client, "The Flint")

        # Play a hand and compare scores
        state = client.call("gamestate")
        predicted = your_scoring_function(state)
        result = client.call("play", {"cards": [0, 1, 2, 3, 4]})
        actual = result["chips"]
        assert predicted == actual, f"Mismatch: {predicted} vs {actual}"
    finally:
        if server:
            stop_server(server)
```

#### Card Format

Cards use `{Suit}_{Rank}` format: `H_K` = King of Hearts, `S_A` = Ace of Spades, `D_T` = 10 of Diamonds, `C_2` = 2 of Clubs.

#### Available Test Suites

| Test File | What It Tests |
|-----------|--------------|
| `test_batch069_fixes.py` | Four Fingers, Shortcut, Flower Pot, Ox, Luchador edge cases |
| `test_shortcut_edges.py` | Shortcut joker gapped straights, combined with Four Fingers |
| `test_ox.py` | The Ox boss — most-played hand locking and money zeroing |
| `test_boss.py` | General boss blind scoring interactions |
| `test_hook_scaling.py` | Scaling/state-dependent joker scoring under bosses |
| `test_win.py` | Full game win path (uses seed `GODMODE1`) |
| `test_glass_poly.py` | Glass + Polychrome edition x_mult interactions |
| `test_holo_edition.py` | Holographic edition scoring |
| `test_percard_scoring.py` | Per-card scoring breakdown validation |
| `test_rearrange.py` | Card rearrangement visual + logical ordering |

## Architecture

```
balatrobot API  -->  bot.py (game loop)
                       |
                     engine.py (rule engine)
                       |
                     rules/ (state-specific rule sets)
                       |
              +--------+--------+
              |                 |
        domain/policy/    domain/scoring/
        (what to do)      (how much is it worth)
              |                 |
        joker_effects/    strategy.py
        (score simulation) (build affinity)
```

The bot polls `gamestate` from the API, wraps it in a `RoundContext`, then runs the rule engine. Each game state (SELECTING_HAND, SHOP, BLIND_SELECT, etc.) has its own ordered list of rules. The first rule whose conditions match fires an action back to the API.

Scoring is fully simulated — the bot runs every candidate hand through the joker effect pipeline to predict exact chip totals, then picks the best option.
