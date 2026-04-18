"""Thin HTTP client that pushes data to the JackPotts dashboard API.

No-ops when JACKPOTTS_URL is not configured. Game data is buffered and
flushed every FLUSH_INTERVAL seconds as a gzip-compressed bulk POST.
Instance state updates and batch start/finish are sent immediately.
"""

from __future__ import annotations

import atexit
import gzip
import json
import logging
import os
import threading
import time
from typing import List, Optional

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = 10.0
FLUSH_INTERVAL = 120.0  # 2 minutes

# ---------------------------------------------------------------------------
# Game buffer — collects post_game payloads, flushes periodically
# ---------------------------------------------------------------------------

_game_buffer: list[tuple[int, dict]] = []  # [(batch_id, game_data), ...]
_buffer_lock = threading.Lock()
_flush_timer: threading.Timer | None = None


def _url() -> str:
    return os.environ.get("JACKPOTTS_URL", "").rstrip("/")


def _key() -> str:
    return os.environ.get("JACKPOTTS_API_KEY", "")


def _post(path: str, json_data: dict) -> Optional[dict]:
    """POST JSON to the dashboard API. Returns response JSON or None."""
    url = _url()
    if not url:
        return None
    try:
        resp = httpx.post(
            f"{url}{path}",
            json=json_data,
            headers={
                "Authorization": f"Bearer {_key()}",
                "Content-Type": "application/json",
            },
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception:
        logger.warning("Dashboard API call failed: %s", path, exc_info=True)
        return None


def _post_gzip(path: str, data: dict) -> Optional[dict]:
    """POST gzip-compressed JSON to the dashboard API."""
    url = _url()
    if not url:
        return None
    try:
        raw = json.dumps(data).encode("utf-8")
        compressed = gzip.compress(raw)
        ratio = len(compressed) / len(raw) * 100 if raw else 0
        logger.info(
            "Flushing %d bytes -> %d bytes (%.0f%%) to %s",
            len(raw), len(compressed), ratio, path,
        )
        resp = httpx.post(
            f"{url}{path}",
            content=compressed,
            headers={
                "Authorization": f"Bearer {_key()}",
                "Content-Type": "application/json",
                "Content-Encoding": "gzip",
            },
            timeout=30.0,  # bulk payloads may be larger
        )
        resp.raise_for_status()
        return resp.json()
    except Exception:
        logger.warning("Dashboard bulk API call failed: %s", path, exc_info=True)
        return None


def _flush_buffer() -> None:
    """Send all buffered games as a single compressed bulk request."""
    global _flush_timer
    with _buffer_lock:
        if not _game_buffer:
            _schedule_flush()
            return
        pending = list(_game_buffer)
        _game_buffer.clear()

    # Group by batch_id (usually just one)
    by_batch: dict[int, list[dict]] = {}
    for batch_id, game_data in pending:
        by_batch.setdefault(batch_id, []).append(game_data)

    for batch_id, games in by_batch.items():
        logger.info("Flushing %d buffered games for batch %d", len(games), batch_id)
        _post_gzip(f"/api/ingest/batch/{batch_id}/games/bulk", {"games": games})

    _schedule_flush()


def _schedule_flush() -> None:
    """Schedule the next buffer flush (no-op if URL not configured)."""
    global _flush_timer
    if not _url():
        return
    _flush_timer = threading.Timer(FLUSH_INTERVAL, _flush_buffer)
    _flush_timer.daemon = True
    _flush_timer.start()


def _stop_flush_timer() -> None:
    """Cancel the flush timer."""
    global _flush_timer
    if _flush_timer:
        _flush_timer.cancel()
        _flush_timer = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def post_batch_start(
    num_instances: int,
    games_per_inst: int,
    decks: str,
    stake: str = "WHITE",
    batch_number: Optional[int] = None,
) -> Optional[tuple[int, int]]:
    """Create a new batch. Server auto-assigns batch_number when not supplied.

    Returns (batch_id, batch_number) or None on failure.
    """
    payload: dict = {
        "num_instances": num_instances,
        "games_per_instance": games_per_inst,
        "decks": decks,
        "stake": stake,
    }
    if batch_number is not None:
        payload["batch_number"] = batch_number

    result = _post("/api/ingest/batch/start", payload)
    if result and "batch_id" in result and "batch_number" in result:
        return result["batch_id"], result["batch_number"]
    return None


def post_instance_states(batch_id: int, instances: List[dict]) -> None:
    """Bulk upsert instance states (sent immediately, small payload)."""
    _post(f"/api/ingest/batch/{batch_id}/instances", {
        "instances": instances,
    })


def post_value_map(batch_id: int, payload: dict) -> None:
    """Push the joker value-map snapshot for a batch.

    Payload shape: ``{"scenarios": [...], "rows": [{key, name, rarity,
    rarity_label, cost, effect_hint, values: {scenario: float}}, ...]}``.

    Stored per-batch on the dashboard so tuning changes can be compared
    batch-to-batch. No-ops when the dashboard URL is not configured.
    """
    _post_gzip(f"/api/ingest/batch/{batch_id}/value-map", payload)


def post_game(batch_id: int, game_data: dict) -> None:
    """Buffer a completed game for bulk upload."""
    if not _url():
        return
    with _buffer_lock:
        _game_buffer.append((batch_id, game_data))
        # Start the flush timer on first buffered game
        if len(_game_buffer) == 1 and _flush_timer is None:
            _schedule_flush()


def flush_games() -> None:
    """Force-flush any buffered games. Call before process exit."""
    _stop_flush_timer()
    _flush_buffer()
    # _flush_buffer always re-arms the periodic timer; cancel it again so
    # the daemon thread doesn't keep firing after shutdown.
    _stop_flush_timer()


def post_batch_finish(batch_id: int, status: str = "finished") -> None:
    """Mark a batch as finished. Flushes buffered games first.

    Retries on transient network failure. An orphaned "running" batch is much
    worse than a brief delay here — the dashboard has a stale-batch sweep as
    a backstop, but we still want the explicit status (finished vs abandoned)
    to land whenever possible.
    """
    flush_games()
    url = _url()
    if not url:
        return
    payload = {"status": status}
    path = f"/api/ingest/batch/{batch_id}/finish"
    for attempt in range(3):
        try:
            resp = httpx.post(
                f"{url}{path}",
                json=payload,
                headers={
                    "Authorization": f"Bearer {_key()}",
                    "Content-Type": "application/json",
                },
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            return
        except Exception:
            if attempt == 2:
                logger.error("post_batch_finish failed after 3 attempts: %s", path, exc_info=True)
                return
            time.sleep(2 ** attempt)


# Flush on process exit so no games are lost
atexit.register(flush_games)
