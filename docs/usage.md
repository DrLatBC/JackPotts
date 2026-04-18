# Usage

## Start the balatrobot server

Launch a headless Balatro instance with the mod's RPC server:

```bash
uvx balatrobot serve --port 12346
```

Useful flags when invoking directly (the supervisor sets these automatically):

- `--headless` — no visible window
- `--fast` — uncap game speed
- `--love-path` / `--lovely-path` — override binary paths

## Single game

```bash
balatro-bot --port 12346 --start
```

| Flag | Default | Description |
|------|---------|-------------|
| `--port` | 12346 | RPC server port |
| `--start` | off | Start a new game automatically |
| `--deck` | RED | Deck to use |
| `--stake` | WHITE | Stake level |
| `--seed` | random | Force a specific seed |
| `--games` | 1 | Play N games back-to-back |
| `--log` | auto | Path for game log |
| `--verbose` | off | Debug-level logging |
| `--dashboard-batch-id` | none | Attach games to a dashboard batch (set by supervisor) |

## Parallel runs

The supervisor manages N bot+server pairs with health monitoring, auto-restart, and isolated save profiles:

```bash
python -m balatro_bot.supervisor --instances 4
```

The supervisor also handles dashboard batch lifecycle (start → heartbeats → finish) when `JACKPOTTS_URL` is configured.

> **Game must be closed** before running the supervisor — it launches Balatro itself.

## Logs

```
bot_log/<port>/game_NNN.log       # game progression
bot_log/<port>/scoring_NNN.log    # per-hand chip/mult breakdown
bot_log/supervisor.log            # supervisor orchestration
bot_log/wins/                     # copies of winning game logs
```

## Local stats (log-based)

Ad-hoc analysis without a database:

```bash
python -m stats                   # latest batch
python -m stats 055 060           # compare batches
```

For richer analytics, point the bot at a [JackPotts dashboard](dashboard.md) instance.
