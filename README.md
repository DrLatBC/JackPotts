# Jack Potts

[![built with Python](https://img.shields.io/badge/built%20with-Python-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![license MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![version v1.1.0](https://img.shields.io/badge/version-v1.1.0-blue)](https://github.com/DrLatBC/JackPotts/releases)
[![tests 454 passing](https://img.shields.io/badge/tests-454%20passing-brightgreen)](tests/)
[![platform Windows](https://img.shields.io/badge/platform-Windows-lightgrey)](https://github.com/DrLatBC/JackPotts)
[![PRs welcome](https://img.shields.io/badge/PRs-welcome-brightgreen)](https://github.com/DrLatBC/JackPotts/pulls)

Rule-based bot that plays [Balatro](https://store.steampowered.com/app/2379780/Balatro/) autonomously via the [balatrobot](https://github.com/coder/balatrobot) mod's JSON-RPC API. A priority-ordered rule engine evaluates every game state and the first rule that fires wins — no ML, no neural nets, just hand-tuned heuristics backed by combinatorial hand enumeration, Monte Carlo rollouts over future deck draws, and a full simulation of Balatro's scoring pipeline.

Live stats dashboard: **[jackpotts.drlat.dev](https://jackpotts.drlat.dev)**

## Documentation

| Page | What's inside |
|------|---------------|
| [Installation](docs/installation.md) | Prerequisites, the fork rationale, `.env.local` setup |
| [Usage](docs/usage.md) | Running single games, the supervisor, CLI flags, logs |
| [Architecture](docs/architecture.md) | Rule engine, decision flow, module map |
| [Scoring](docs/scoring.md) | Joker pipeline, card/enhancement/edition handling, The Idol mod patch |
| [Dashboard](docs/dashboard.md) | JackPotts ingest pipeline, payload shape, batch lifecycle |
| [Testing](docs/testing.md) | Unit tests, integration harness, writing new tests |
| [Release notes](docs/release-notes.md) | Changelog |

## Quickstart

```bash
git clone https://github.com/DrLatBC/JackPotts.git
cd JackPotts
pip install -e ".[dev,runtime]"
```

Create `.env.local` in the repo root:

```ini
BALATRO_EXE=G:\SteamLibrary\steamapps\common\Balatro\Balatro.exe
LOVELY_DLL=G:\SteamLibrary\steamapps\common\Balatro\version.dll
```

Run a single game:

```bash
uvx balatrobot serve --port 12346          # terminal 1
balatro-bot --port 12346 --start           # terminal 2
```

Run 4 parallel instances with health monitoring:

```bash
python -m balatro_bot.supervisor --instances 4
```

See [Installation](docs/installation.md) and [Usage](docs/usage.md) for full details.

## How it works

```
balatrobot API  -->  bot.py (game loop)
                       |
                     engine.py (priority-ordered rules)
                       |
              +--------+---------+
              |                  |
        domain/policy/     domain/scoring/
        (what to do)       (how much is it worth)
              |                  |
        joker_effects/     strategy.py
        (score simulation) (build affinity)
```

Every candidate hand is run through the full scoring simulation — joker effects, enhancements, editions, seals, retriggers, boss blind modifiers — before the bot picks one. Scoring accuracy against the game's actual chip totals sits at **99.89%**.

See [Architecture](docs/architecture.md) for the full breakdown.

## Contributing

PRs welcome. The integration harness ([docs/testing.md](docs/testing.md)) makes it easy to reproduce scoring mismatches against a live Balatro instance — please include a failing test case with bug reports where feasible.
