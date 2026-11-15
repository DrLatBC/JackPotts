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

## Optional: PyPy for simulator batches

Simulator batches (`--sim`) run their bots on [PyPy](https://pypy.org) when `.venv-pypy` exists, about 1.8x the games per hour with identical play ([ADR 0010](adr/0010-simulator-bots-run-on-pypy.md)). Without it they run on the normal environment. Unpack PyPy 3.12 (v8.0.1 or later) anywhere, then from the repo root:

```powershell
& "C:\path\to\pypy3.12-v8.0.1-win64\pypy.exe" -m venv .venv-pypy
.venv-pypy\Scripts\python.exe -m pip install httpx "typer==0.15.4" pytest
.venv-pypy\Scripts\python.exe -m pip install --no-deps --ignore-requires-python -e ..\balatrobot
.venv-pypy\Scripts\python.exe -m pip install --no-deps -e .
```

`typer` is pinned because newer releases need a compiled dependency with no PyPy build, and the fork is installed past its Python 3.13 floor because the bot only uses its client. `$BOT_PYPY` points the supervisor at a different PyPy interpreter. Check the bot still runs there with:

```powershell
.venv-pypy\Scripts\python.exe -m pytest -q --ignore=tests/test_gui.py --ignore=tests/test_supervisor_job.py --ignore=tests/test_dashboard_client.py
```

## Optional: the compiled scoring kernel

`kernel/` holds hand search and scoring in Rust. With `BALATRO_KERNEL=rust` in the environment the bot asks it instead of the Python scorer; the two give the same answers (`tests/test_kernel_differential.py`), the compiled one about 85 times faster per search. Without the variable nothing changes.

It needs Rust (`winget install --id Rustlang.Rustup -e`) and, on Windows, Visual Studio's "Desktop development with C++" workload with a Windows SDK. Build it into the bot's environment from the repo root:

```powershell
$env:VIRTUAL_ENV = "$PWD\.venv"
.venv\Scripts\python.exe -m pip install maturin
Set-Location kernel\py
..\..\.venv\Scripts\maturin.exe develop --release
Set-Location ..\..
```

Rebuild after any change under `kernel/`. Checks:

```powershell
.venv\Scripts\python.exe -m pytest -q tests/test_kernel_differential.py
.venv\Scripts\python.exe scripts/kernel_soak.py targeted
.venv\Scripts\python.exe scripts/kernel_mutations.py
```

The first compares both scorers on generated hands, the second does the same over every joker with every card enhancement, and the third plants bugs in the kernel to confirm those comparisons notice. The kernel runs on CPython only, so PyPy bots keep the Python scorer.
