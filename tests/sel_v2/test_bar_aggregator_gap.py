"""Bar-aggregator gap-recovery tests (audit P1-4).

A WS disconnect loses a bar's trades; the aggregator used to silently skip empty bars,
leaving a permanent hole in v2_bars_4h that desyncs every rolling-window feature (the paper
engine needs ≥180 contiguous bars). It now recovers the bar from the official OKX candle and
only logs an explicit GAP when even that fails.
"""
import asyncio
from datetime import datetime, timezone, timedelta

import pytest

import sel_v2.data.v2_bar_aggregator as agg


START = datetime(2024, 1, 1, 4, 0, 0, tzinfo=timezone.utc)
START_MS = int(START.timestamp() * 1000)


# ── pure helpers ────────────────────────────────────────────────────────────

def test_build_bar_row_ohlcv_and_vwap():
    ticks = [{"price": 100.0, "size": 1.0}, {"price": 110.0, "size": 1.0},
             {"price": 90.0, "size": 2.0}]
    row = agg.build_bar_row(ticks, START, "BTC-USDT")
    # (time, symbol, open, high, low, close, volume, vwap, tick_count)
    assert row[2] == 100.0 and row[3] == 110.0 and row[4] == 90.0 and row[5] == 90.0
    assert row[6] == 4.0
    assert row[7] == pytest.approx((100 + 110 + 180) / 4.0)
    assert row[8] == 3


def test_build_bar_row_none_on_empty():
    assert agg.build_bar_row([], START, "BTC-USDT") is None


def test_vwap_is_null_when_unknown():
    # zero-volume bar → vwap NULL (unknown), not a fake 0.0 (P2-5)
    row = agg.build_bar_row([{"price": 100.0, "size": 0.0}], START, "BTC-USDT")
    assert row[7] is None
    # REST-recovered candle carries no VWAP → NULL
    candles = [[str(START_MS), "100", "120", "80", "110", "5", "0", "0", "0"]]
    assert agg.parse_rest_candle(candles, START, "BTC-USDT")[7] is None


def test_parse_rest_candle_matches_start_and_flags_recovered():
    candles = [[str(START_MS), "100", "120", "80", "110", "5", "0", "0", "0"]]
    row = agg.parse_rest_candle(candles, START, "BTC-USDT")
    assert row is not None
    assert row[2] == 100.0 and row[3] == 120.0 and row[5] == 110.0 and row[6] == 5.0
    assert row[8] == 0   # tick_count=0 marks a REST-recovered bar


def test_parse_rest_candle_none_when_no_match():
    candles = [[str(START_MS + 4 * 3600 * 1000), "1", "1", "1", "1", "1", "0", "0", "0"]]
    assert agg.parse_rest_candle(candles, START, "BTC-USDT") is None


# ── aggregate_bar gap path ──────────────────────────────────────────────────

class _Conn:
    def __init__(self, ticks):
        self._ticks = ticks
        self.inserted = []

    async def fetch(self, *a, **k):
        return self._ticks

    async def execute(self, *a, **k):
        self.inserted.append(a)


class _Pool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        conn = self._conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *a):
                return False
        return _Ctx()


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._payload


class _Session:
    def __init__(self, payload):
        self._payload = payload
        self.called = False

    def get(self, url, **kw):
        self.called = True
        return _Resp(self._payload)


def test_empty_bar_recovers_from_rest(monkeypatch):
    monkeypatch.setattr(agg, "SYMBOL", "BTC-USDT")
    conn = _Conn(ticks=[])
    payload = {"code": "0", "data": [[str(START_MS), "100", "120", "80", "110", "5", "0", "0", "0"]]}
    session = _Session(payload)
    asyncio.run(agg.aggregate_bar(_Pool(conn), START, START + timedelta(hours=4), session=session))
    assert session.called
    assert len(conn.inserted) == 1          # the gap was filled, not skipped
    # inserted row carries tick_count=0 (REST-recovered)
    assert conn.inserted[0][-1] == 0


def test_empty_bar_no_session_logs_gap_and_skips(monkeypatch):
    monkeypatch.setattr(agg, "SYMBOL", "BTC-USDT")
    conn = _Conn(ticks=[])
    asyncio.run(agg.aggregate_bar(_Pool(conn), START, START + timedelta(hours=4), session=None))
    assert conn.inserted == []              # nothing inserted, but it's an explicit GAP log now


def test_normal_bar_uses_ticks_not_rest(monkeypatch):
    monkeypatch.setattr(agg, "SYMBOL", "BTC-USDT")
    conn = _Conn(ticks=[{"price": 100.0, "size": 1.0}, {"price": 102.0, "size": 1.0}])
    session = _Session({"code": "0", "data": []})
    asyncio.run(agg.aggregate_bar(_Pool(conn), START, START + timedelta(hours=4), session=session))
    assert not session.called               # ticks present → no REST call
    assert conn.inserted[0][-1] == 2        # tick_count=2
