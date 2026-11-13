# Installation

## Prerequisites

- **Balatro** (Steam)
- **[Lovely](https://github.com/ethangreen-dev/lovely-injector)** mod loader for Balatro
- **[Steamodded](https://github.com/Steamodded/smods)** mod framework — required by balatrobot
- **[balatrobot mod](https://github.com/DrLatBC/balatrobot)** (our fork) installed into Balatro — provides the JSON-RPC server the bot talks to
- **Python 3.13+**

> **Why our fork?** Jack Potts requires API fields and fixes not yet in [upstream balatrobot](https://github.com/coder/balatrobot): edition scoring values, joker ability data, The Ox's locked hand, Ancient Joker suit, The Idol's round-target card, boss blind forcing, endless mode support, and several endpoint hang fixes. A [PR is open](https://github.com/coder/balatrobot/pull/181) to merge most of these upstream — once accepted, the official mod will work for those changes. Until then, use our fork.

## Install

```bash
git clone https://github.com/DrLatBC/JackPotts.git
cd JackPotts
pip install -e ".[dev,runtime]"
pip install -e ../balatrobot     # our fork, cloned next to this repo
```

The `dev` extra adds `pytest` and related tooling. Installing the fork editable provides both the `balatrobot` client library and the `balatrobot serve` CLI the supervisor launches, so fork changes apply without reinstalling. Don't run the server through `uvx balatrobot`: that pulls the latest PyPI release, which can drift ahead of the fork's Lua mod installed in Balatro.

## Environment config

Create `.env.local` in the repo root (gitignored, loaded automatically):

```ini
BALATRO_EXE=G:\SteamLibrary\steamapps\common\Balatro\Balatro.exe
LOVELY_DLL=G:\SteamLibrary\steamapps\common\Balatro\version.dll
```

Adjust paths to match your Steam library. The supervisor finds `balatrobot` next to the Python running it; set `BALATROBOT_EXE` only to point at a different install.

### Optional: JackPotts dashboard

To push per-game data to a dashboard instance, add:

```ini
JACKPOTTS_URL=https://your-dashboard.example
JACKPOTTS_API_KEY=...
```

Leaving these blank disables the dashboard client entirely — the bot runs the same, just without remote reporting. See [dashboard.md](dashboard.md) for details.

## Verify install

```bash
pytest -q
```

All 380 unit tests should pass without a running game.
