# Jack Potts

[![built with Python](https://img.shields.io/badge/built%20with-Python-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![kernel Rust](https://img.shields.io/badge/kernel-Rust-B7410E?logo=rust&logoColor=white)](kernel/)
[![license MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![version v2.0.0](https://img.shields.io/badge/version-v2.0.0-blue)](https://github.com/DrLatBC/JackPotts/releases)
[![tests 842 passing](https://img.shields.io/badge/tests-842%20passing-brightgreen)](tests/)
[![platform Windows](https://img.shields.io/badge/platform-Windows-lightgrey)](https://github.com/DrLatBC/JackPotts)

Two players for [Balatro](https://store.steampowered.com/app/2379780/Balatro/), and the tools to run them by the thousand.

- **The bot**: a rule-based player. A priority-ordered rule engine picks every move, backed by a full simulation of Balatro's scoring, combinatorial hand search and Monte Carlo rollouts over future draws. It plays the real game through the [balatrobot](https://github.com/coder/balatrobot) mod's JSON-RPC API, or the [Jackdaw](https://github.com/TylerFlar/jackdaw-balatro) simulator, about 24,000 games an hour across 12 instances.
- **The model**: a neural network that started as a copy of the bot, learned from its games, then taught itself by playing the simulator. It now wins more than twice as often as the bot it copied.

Live stats dashboard: **[jackpotts.drlat.dev](https://jackpotts.drlat.dev)**

## Results

Blue Deck, White Stake, in the Jackdaw simulator, each player taking its most likely move. The model's numbers are on the same 5,000 evaluation seeds.

| Player | Wins | Mean final ante |
|---|---|---|
| The bot, over a 24,000-game batch | about 14% | – |
| The model, copying the bot (`b187`) | 4.5% | 3.96 |
| ...after its first hour of learning by playing | 8.9% | 4.63 |
| ...after a 9-hour run | 22.8% | 5.70 |
| ...after a further 6 hours | **33.8%** | **6.23** |

Jackdaw assumes every card is unlocked, so its shops and packs differ from a live profile's; these are simulator numbers.

## The control panel

`.venv\Scripts\pythonw.exe -m balatro_bot.gui` opens the desktop panel. It runs everything below windowless, and closing it never stops a run.

- **Train**: start, stop, extend and compare learning runs. Pick where a run starts (a copy or any saved update), the method, the win bonus, deck and stake. Charts follow points per game, agreement with the start, and evaluation win rate and ante. Each finished run is checked against its start on 5,000 seeds, seed by seed.
- **Stream**: a model plays the real, visible game, move by move, with an [OBS](https://obsproject.com/) overlay showing what it weighed before each move.
- **Batches**: run the bot on many instances at once, in the real game or the simulator. Add or remove instances mid-batch, pause and resume, and let some instances explore runner-up shop and pack choices.

## Quickstart

```powershell
git clone https://github.com/DrLatBC/JackPotts.git
cd JackPotts
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev,runtime]"
```

Create `.env.local` in the repo root:

```ini
BALATRO_EXE=G:\SteamLibrary\steamapps\common\Balatro\Balatro.exe
LOVELY_DLL=G:\SteamLibrary\steamapps\common\Balatro\version.dll
```

Then open the control panel, or run one game from two terminals:

```powershell
balatrobot serve --port 12346
```

```powershell
balatro-bot --port 12346 --start
```

[Installation](docs/installation.md) covers the balatrobot fork, PyPy for simulator batches and building the Rust kernel; [Usage](docs/usage.md) the command line.

## How the bot works

```
balatrobot API / Jackdaw  -->  bot.py (game loop)
                                 |
                               engine.py (priority-ordered rules)
                                 |
                        +--------+---------+
                        |                  |
                  domain/policy/     domain/scoring/
                  (what to do)       (how much it is worth)
                        |                  |
                  joker_effects/     strategy.py
                  (score simulation) (build affinity)
```

Every candidate hand runs through the full scoring simulation (joker effects, enhancements, editions, seals, retriggers, boss blinds) before the bot picks one. Measured against the game's own chip totals, its accuracy is 99.89% across the integration test corpus. The shop is one evaluator that scores whole plans (sell this, buy that, reroll, leave) against a money budget that changes with the ante.

For volume, hand search and scoring also exist as a compiled Rust kernel (`kernel/`), which gives the same answers as the Python scorer about 85 times faster per search. Simulator games are kept as compact replays, which is also what the model learns from.

See [Architecture](docs/architecture.md) and [Scoring](docs/scoring.md).

## How the model learns

Code in `rl/`, run from a separate environment (Python 3.13, the Jackdaw wheel in `sim/wheels/`, PyTorch for the CPU):

```powershell
py -3.13 -m venv .venv-rl
.venv-rl\Scripts\python.exe -m pip install sim\wheels\jackdaw-0.2.6-cp312-abi3-win_amd64.whl httpx pytest
.venv-rl\Scripts\python.exe -m pip install torch --index-url https://download.pytorch.org/whl/cpu
```

1. **Copy the bot.** `rl.convert` turns the bot's simulator replays into examples, and `rl.train` fits a small transformer to choose what the bot chose: the action type, the item it points at, and the set of hand cards.
2. **Learn by playing.** `rl.ppo train` lets the copy play Jackdaw and keeps what works. A run scores a game by the bosses it beats plus a bonus for the win, and learns in one of two ways:
   - *Same seed*: each seed is played four times and every game is judged against the other plays of its seed, which cancels most of the seed's luck.
   - *Branches*: the game is copied at a decision, a different move is played on the copy, and both lines play to the end. The move that did better is learned at that state, one decision at a time.
3. **Watch it.** The Stream tab puts any saved update in front of the real game.

## Documentation

| Page | What's inside |
|------|---------------|
| [Installation](docs/installation.md) | Prerequisites, the balatrobot fork, `.env.local`, PyPy, the Rust kernel |
| [Usage](docs/usage.md) | Single games, the supervisor, CLI flags, logs |
| [Architecture](docs/architecture.md) | Rule engine, decision flow, module map |
| [Scoring](docs/scoring.md) | Joker pipeline, card, enhancement and edition handling |
| [Dashboard](docs/dashboard.md) | What the bot sends the JackPotts dashboard, batch lifecycle |
| [Testing](docs/testing.md) | Unit tests, the integration harness, writing new tests |
| [Release notes](docs/release-notes.md) | Changelog |
| [Glossary](GLOSSARY.md) | The project's terms: milk, chase, finisher, affinity, economy phase, … |
| [Decisions](docs/adr/) | Why the bot and the simulator setup are built the way they are |

## Contributing

PRs welcome. The integration harness ([docs/testing.md](docs/testing.md)) reproduces scoring mismatches against a live Balatro instance; please include a failing test case with bug reports where you can.
