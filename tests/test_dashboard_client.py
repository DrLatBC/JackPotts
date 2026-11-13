"""Dashboard game uploads: failed bulk posts are kept and retried, not dropped."""

from __future__ import annotations

import httpx
import pytest

from balatro_bot import dashboard_client as dc


class _Resp:
    def __init__(self, status: int):
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            req = httpx.Request("POST", "http://dash/x")
            raise httpx.HTTPStatusError("err", request=req, response=httpx.Response(self.status_code, request=req))

    def json(self):
        return {"ok": True}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("JACKPOTTS_URL", "http://dash")
    monkeypatch.setattr(dc, "_schedule_flush", lambda: None)
    monkeypatch.setattr(dc.time, "sleep", lambda s: None)
    dc._game_buffer.clear()
    calls: list[int] = []

    def use(statuses):
        it = iter(statuses)

        def fake_post(url, **kw):
            calls.append(1)
            s = next(it)
            if s is None:
                raise httpx.ConnectError("down")
            return _Resp(s)

        monkeypatch.setattr(dc.httpx, "post", fake_post)
        return calls

    yield use
    dc._game_buffer.clear()


def _buffer(n: int, batch_id: int = 7) -> None:
    dc._game_buffer.extend((batch_id, {"seed": f"S{i}"}) for i in range(n))


def test_success_empties_buffer(client):
    client([200])
    _buffer(3)
    assert dc._flush_buffer() is True
    assert dc._game_buffer == []


@pytest.mark.parametrize("failure", [None, 502, 429])
def test_transient_failure_keeps_games_in_order(client, failure):
    client([failure])
    _buffer(3)
    assert dc._flush_buffer() is False
    assert [g["seed"] for _, g in dc._game_buffer] == ["S0", "S1", "S2"]


def test_rejected_payload_is_dropped_not_retried(client):
    client([422])
    _buffer(2)
    assert dc._flush_buffer() is True
    assert dc._game_buffer == []


def test_retried_games_go_ahead_of_newer_ones(client):
    client([None])
    _buffer(2)
    dc._flush_buffer()
    dc._game_buffer.append((7, {"seed": "NEW"}))
    assert [g["seed"] for _, g in dc._game_buffer] == ["S0", "S1", "NEW"]


def test_buffer_cap_drops_oldest(client, monkeypatch):
    monkeypatch.setattr(dc, "MAX_BUFFERED_GAMES", 3)
    client([None])
    _buffer(5)
    dc._flush_buffer()
    assert [g["seed"] for _, g in dc._game_buffer] == ["S2", "S3", "S4"]


def test_flush_games_retries_until_delivered(client):
    calls = client([None, None, 200])
    _buffer(2)
    dc.flush_games()
    assert len(calls) == 3
    assert dc._game_buffer == []
