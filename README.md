# Balatro Bot

Rule-based bot that plays [Balatro](https://store.steampowered.com/app/2379780/Balatro/) autonomously via the [balatrobot](https://github.com/your-repo/balatrobot) mod's JSON-RPC API. It evaluates every game state through a priority-ordered rule engine, picking the first rule that fires — no ML, no tree search, just hand-tuned heuristics.

## Prerequisites

- **Balatro** (Steam)
- **balatrobot mod** installed into Balatro (provides the JSON-RPC server the bot talks to)
- **Python 3.11+**

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

Integration tests (requires a running `balatrobot serve` instance):

```bash
python tests/integration/test_scoring_bugs.py --port 12346
```

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
