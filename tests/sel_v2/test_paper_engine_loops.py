"""Tests for the PaperEngine sub-bar loops (P1-4): Strategy-2 tick loop,
position-management risk monitor, and open-position restore.

The loops themselves are infinite; their per-iteration logic is factored into
testable helpers, exercised here with fake asyncpg/redis so no DB is needed."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from sel_v2.paper.paper_engine import PaperEngine, PAPER_HARD_STOP_PCT


# ── fakes ─────────────────────────────────────────────────────────────────────

class _FakeConn:
    def __init__(self, *, fetchval=None, fetch_rows=None):
        self._fetchval = fetchval        # callable(sql, *args) or value
        self._fetch_rows = fetch_rows or []

    async def fetchval(self, sql, *args):
        v = self._fetchval
        return v(sql, *args) if callable(v) else v

    async def fetch(self, sql, *args):
        return self._fetch_rows


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        conn = self._conn

        class _Ctx:
            async def __aenter__(self_inner):
                return conn

            async def __aexit__(self_inner, *a):
                return False
        return _Ctx()


class _FakeRedis:
    def __init__(self):
        self.store = {}

    async def set(self, k, v):
        self.store[k] = v


def _engine_with(conn=None, redis=None) -> PaperEngine:
    e = PaperEngine()
    if conn is not None:
        e._pool = _FakePool(conn)
    e._redis = redis
    return e


def _ts(h):
    return datetime(2026, 6, 1, h, tzinfo=timezone.utc)


# ── Strategy-2 tick loop: new-tick detection ──────────────────────────────────

@pytest.mark.asyncio
async def test_tick_loop_reprocesses_only_on_new_ticks(monkeypatch):
    e = _engine_with(_FakeConn(fetchval=lambda *a: _ts(8)))
    calls = []

    async def fake_reprocess():
        calls.append(1)
    monkeypatch.setattr(e, "_reprocess", fake_reprocess)

    # first observation of a tick → reprocess, cursor advances
    assert await e._maybe_reprocess_on_new_ticks() is True
    assert e._last_tick_ts == _ts(8)
    assert len(calls) == 1

    # same latest tick → no reprocess
    assert await e._maybe_reprocess_on_new_ticks() is False
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_tick_loop_no_ticks_is_noop(monkeypatch):
    e = _engine_with(_FakeConn(fetchval=lambda *a: None))
    monkeypatch.setattr(e, "_reprocess", _raise_if_called)
    assert await e._maybe_reprocess_on_new_ticks() is False


async def _raise_if_called():
    raise AssertionError("_reprocess must not run when there are no new ticks")


# ── Position-risk snapshot (pure) ─────────────────────────────────────────────

def test_risk_snapshot_unrealized_pnl_and_breach():
    e = PaperEngine()
    positions = [
        {"strategy": "strategy_1", "sub_account": "subaccount_1", "direction": "LONG",
         "entry_price": 100.0, "notional_usdt": 1000.0},
        {"strategy": "strategy_2", "sub_account": "subaccount_2", "direction": "SHORT",
         "entry_price": 100.0, "notional_usdt": 500.0},
    ]
    snap = e._position_risk_snapshot(positions, mark=90.0)
    # long down 10% → -100; short up (price fell) +10% → +50
    assert snap["positions"][0]["unrealized_pnl_usdt"] == pytest.approx(-100.0)
    assert snap["positions"][1]["unrealized_pnl_usdt"] == pytest.approx(50.0)
    assert snap["total_unrealized_usdt"] == pytest.approx(-50.0)
    # the long breached the hard-stop (10% > 5% default), the short did not
    assert len(snap["breaches"]) == 1
    assert snap["breaches"][0]["direction"] == "LONG"


def test_risk_snapshot_no_breach_within_threshold():
    e = PaperEngine()
    pos = [{"strategy": "s", "sub_account": "a", "direction": "LONG",
            "entry_price": 100.0, "notional_usdt": 1000.0}]
    snap = e._position_risk_snapshot(pos, mark=100.0 * (1 - PAPER_HARD_STOP_PCT / 2))
    assert snap["breaches"] == []


@pytest.mark.asyncio
async def test_publish_position_risk_writes_redis_and_alerts():
    conn = _FakeConn(fetchval=lambda sql, *a: 90.0)   # latest mark
    rds = _FakeRedis()
    e = _engine_with(conn, rds)
    e._engine = None
    e._open_positions = [{
        "strategy": "strategy_1", "sub_account": "subaccount_1", "direction": "LONG",
        "entry_price": 100.0, "size": 1000.0, "leverage": 1.0,
    }]
    snap = await e._publish_position_risk()
    assert snap["n_open"] == 1
    assert "v2:paper:position_risk" in rds.store
    # 10% loss → alert published
    assert "v2:paper:risk_alert" in rds.store
    alert = json.loads(rds.store["v2:paper:risk_alert"])
    assert alert[0]["direction"] == "LONG"


@pytest.mark.asyncio
async def test_publish_position_risk_no_mark_returns_none():
    e = _engine_with(_FakeConn(fetchval=lambda *a: None), _FakeRedis())
    assert await e._publish_position_risk() is None


# ── Open-position restore ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_load_open_positions_from_db():
    rows = [{"id": "x", "strategy": "strategy_1", "sub_account": "subaccount_1",
             "direction": "LONG", "entry_price": 100.0, "size": 1000.0, "leverage": 2.0,
             "instrument": "BTC-USDT", "entry_time": _ts(0), "entry_state": "Coiling"}]
    e = _engine_with(_FakeConn(fetch_rows=rows))
    await e._load_open_positions()
    assert len(e._open_positions) == 1
    # the restored snapshot feeds the monitor when the engine hasn't replayed yet
    e._engine = None
    cur = e._current_open_positions()
    assert cur[0]["notional_usdt"] == pytest.approx(2000.0)   # size * leverage
    assert cur[0]["direction"] == "LONG"


@pytest.mark.asyncio
async def test_latest_mark_falls_back_to_bar_close():
    def fv(sql, *a):
        return None if "v2_ticks" in sql else 31000.0
    e = _engine_with(_FakeConn(fetchval=fv))
    assert await e._latest_mark() == pytest.approx(31000.0)
