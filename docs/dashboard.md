# Dashboard integration

Jack Potts can push per-game data to an HTTP ingest endpoint for aggregate analytics — win rates by deck/stake, joker pick distributions, shop behavior, ante-by-ante scoring, and so on. The bot runs the same without it.

The author's live dashboard (a FastAPI + Postgres + HTMX app) is publicly viewable at **[jackpotts.drlat.dev](https://jackpotts.drlat.dev)** — the source for that app is kept private, so this page documents the client side only. Useful if you're curious about the payload shape or want to wire the bot up to a compatible ingest service of your own.

## Config

Set these in `.env.local` to enable reporting:

```ini
JACKPOTTS_URL=https://your-ingest-endpoint.example
JACKPOTTS_API_KEY=...
```

When either is unset, `dashboard_client.py` no-ops everything and the bot runs purely locally.

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

**Rich arrays:** `rounds[]`, `jokers[]`, `hand_types[]`, `consumables[]`, `actions_log[]`, `ante_snapshots[]`, `hand_scores[]`, `shop_events[]`.
