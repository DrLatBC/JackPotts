# Dashboard integration

Jack Potts can push per-game data to a [JackPotts](https://github.com/DrLatBC/jackpotts-dashboard) dashboard instance for aggregate analytics — win rates by deck/stake, joker pick distributions, shop behavior, ante-by-ante scoring, and so on. The dashboard is a separate repo (FastAPI + Postgres + HTMX). The bot runs the same without it.

Live reference instance: **[jackpotts.drlat.dev](https://jackpotts.drlat.dev)**.

## Config

Add to `.env.local`:

```ini
JACKPOTTS_URL=https://your-dashboard.example
JACKPOTTS_API_KEY=...
```

When unset, `dashboard_client.py` no-ops everything.

## Data flow

```
supervisor.py
  --> dashboard_client.post_batch_start()            # creates batch, gets batch_id
  --> spawns N bot instances with --dashboard-batch-id

bot.py (per game)
  --> GameLoopState collects action_log, ante_snapshots,
      hand_scores, shop_events, joker tracking, hand types
  --> dashboard_client.post_game(batch_id, data)     # buffers in memory

dashboard_client (background)
  --> _flush_buffer()                                # gzip bulk POST every 120s

supervisor.py (on finish)
  --> dashboard_client.post_batch_finish()           # flushes + marks done
```

## Client API (`src/balatro_bot/dashboard_client.py`)

| Call | Endpoint |
|------|----------|
| `post_batch_start(batch_number, num_instances, games_per_inst, decks, stake)` -> `batch_id` | `POST /api/ingest/batch/start` |
| `post_instance_states(batch_id, instances)` | `POST /api/ingest/batch/{batch_id}/instances` |
| `post_game(batch_id, game_data)` | buffered locally, no HTTP call |
| `flush_games()` | `POST /api/ingest/batch/{batch_id}/games/bulk` (gzip) |
| `post_batch_finish(batch_id)` | `POST /api/ingest/batch/{batch_id}/finish` |

Games are buffered and flushed as gzip-compressed bulk uploads every 120 seconds (or on exit via `atexit`).

## Payload shape

Built in `bot.py` — search for `post_game(`.

**Core fields:** `seed`, `deck`, `stake`, `win`, `final_ante`, `final_round`, `actions`, `rerolls`, `pack_picks`, `pack_skips`, `final_money`, `final_jokers`, `total_hands`, `total_discards`, `log_text` (wins only).

**Rich arrays:** `rounds[]`, `jokers[]`, `hand_types[]`, `consumables[]`, `actions_log[]`, `ante_snapshots[]`, `hand_scores[]`, `shop_events[]`. Each element maps to a row in the corresponding dashboard table.

## Running your own instance

See the dashboard repo's README for hosting setup. For quick experimentation you can point `JACKPOTTS_URL` at a local dev server (`http://localhost:8000`) and iterate against the dashboard repo's FastAPI app directly.
