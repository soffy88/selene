"""Tests for sel_v2.data.ofi_persister — the point-in-time OFI feature store (P1-5)."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock

import pytest

from sel_v2.data.ofi_persister import merge_ofi_rows, persist_ofi, _UPSERT_SQL


def _b(h):
    return datetime(2026, 6, 1, h, tzinfo=timezone.utc)


def test_merge_combines_flow_and_lob_sorted():
    flow = [{"b": _b(8), "net": 5.0, "vol": 100.0},
            {"b": _b(0), "net": -2.0, "vol": 50.0}]
    lob = [{"b": _b(8), "imb": 1.5}]
    rows = merge_ofi_rows(flow, lob)
    assert [r["time"] for r in rows] == [_b(0), _b(8)]      # ascending
    assert rows[0]["taker_net"] == -2.0 and rows[0]["lob_imb"] is None
    assert rows[1]["taker_net"] == 5.0 and rows[1]["lob_imb"] == 1.5


def test_merge_lob_only_bucket_leaves_flow_none():
    rows = merge_ofi_rows([], [{"b": _b(4), "imb": -0.3}])
    assert rows == [{"time": _b(4), "taker_net": None, "taker_vol": None, "lob_imb": -0.3}]


class _FakeConn:
    def __init__(self, flow, lob):
        self._flow, self._lob = flow, lob
        self.upserted = None

    async def fetch(self, sql, symbol, since):
        return self._flow if "v2_ticks" in sql else self._lob

    async def executemany(self, sql, rows):
        self.upserted = (sql, list(rows))


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


@pytest.mark.asyncio
async def test_persist_upserts_expected_columns():
    flow = [{"b": _b(0), "net": 3.0, "vol": 80.0}]
    lob = [{"b": _b(0), "imb": 0.7}]
    conn = _FakeConn(flow, lob)
    n = await persist_ofi(_FakePool(conn), symbol="BTC-USDT", since=_b(0) - timedelta(days=1))

    assert n == 1
    sql, rows = conn.upserted
    assert sql == _UPSERT_SQL
    # (time, symbol, taker_net, taker_vol, lob_imb)
    assert rows == [(_b(0), "BTC-USDT", 3.0, 80.0, 0.7)]


@pytest.mark.asyncio
async def test_persist_no_data_writes_nothing():
    conn = _FakeConn([], [])
    n = await persist_ofi(_FakePool(conn), symbol="BTC-USDT", since=_b(0))
    assert n == 0
    assert conn.upserted is None
