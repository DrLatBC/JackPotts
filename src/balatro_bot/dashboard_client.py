"""Thin HTTP client that pushes data to the JackPotts dashboard API.

No-ops when JACKPOTTS_URL is not configured. All calls are fire-and-forget
with a 5s timeout — never blocks the supervisor or raises exceptions.
"""

from __future__ import annotations

import logging
import os
from typing import Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = 5.0


def _url() -> str:
    return os.environ.get("JACKPOTTS_URL", "").rstrip("/")


def _key() -> str:
    return os.environ.get("JACKPOTTS_API_KEY", "")


def _post(path: str, json: dict) -> Optional[dict]:
    """POST to the dashboard API. Returns response JSON or None on failure."""
    url = _url()
    if not url:
        return None
    try:
        resp = httpx.post(
            f"{url}{path}",
            json=json,
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


def post_batch_start(
    batch_number: int,
    num_instances: int,
    games_per_inst: int,
    decks: str,
    stake: str = "WHITE",
) -> Optional[int]:
    """Create a new batch. Returns batch_id or None on failure."""
    result = _post("/api/ingest/batch/start", {
        "batch_number": batch_number,
        "num_instances": num_instances,
        "games_per_instance": games_per_inst,
        "decks": decks,
        "stake": stake,
    })
    if result:
        return result.get("batch_id")
    return None


def post_instance_states(batch_id: int, instances: List[dict]) -> None:
    """Bulk upsert instance states."""
    _post(f"/api/ingest/batch/{batch_id}/instances", {
        "instances": instances,
    })


def post_game(batch_id: int, game_data: dict) -> None:
    """Submit a completed game with rounds, jokers, and hand types."""
    _post(f"/api/ingest/batch/{batch_id}/game", game_data)


def post_batch_finish(batch_id: int, status: str = "finished") -> None:
    """Mark a batch as finished."""
    _post(f"/api/ingest/batch/{batch_id}/finish", {
        "status": status,
    })
