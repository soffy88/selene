"""Unit tests for the v2_cusum_events wiring (orphan-table fix).

CUSUM-Short (S2, engine-owned) and CUSUM-Mid (S1, from the entry filter's reported C±)
both cross their thresholds in normal operation, but `write_cusum_event` had no caller so
`v2_cusum_events` stayed empty and misled two rounds of diagnosis. These tests pin the two
halves of the fix without a live DB:

  1. PaperStrategyEngine._collect_cusum_cross — the cross-detection that mirrors
     CUSUMShort.update's trigger judgment (C+>h → 'up', C->h → 'down', dominant on tie),
     with the threshold>0 guard for the CUSUM-Mid cold-start default.
  2. DBWriter.write_cusum_events_bulk — the idempotent upsert (verified here against a
     recording fake connection; the ON CONFLICT round-trip is exercised live in the deploy
     step against the real unique index).
"""

import asyncio

from sel_v2.paper.strategy_engine import PaperStrategyEngine
from sel_v2.strategies.db_writer import DBWriter


def _engine_stub():
    e = PaperStrategyEngine.__new__(PaperStrategyEngine)
    e._cusum_events = []
    return e


# ── _collect_cusum_cross ───────────────────────────────────────────────────────


def test_positive_cross_records_up():
    e = _engine_stub()
    e._collect_cusum_cross("2026-07-11T00:00:00+00:00", "short", 2.5, 0.0, 2.0)
    assert len(e._cusum_events) == 1
    ts, ctype, direction, peak, thresh, zwin = e._cusum_events[0]
    assert (ctype, direction, peak, thresh, zwin) == ("short", "up", 2.5, 2.0, None)


def test_negative_cross_records_down():
    e = _engine_stub()
    e._collect_cusum_cross("2026-07-11T04:00:00+00:00", "mid", 0.0, 3.1, 1.69)
    assert e._cusum_events[0][2:4] == ("down", 3.1)


def test_both_cross_takes_dominant_excursion():
    e = _engine_stub()
    # C- (2.9) > C+ (2.1) → the negative excursion dominates, as in CUSUMShort.update
    e._collect_cusum_cross("2026-07-11T08:00:00+00:00", "short", 2.1, 2.9, 2.0)
    assert len(e._cusum_events) == 1
    assert e._cusum_events[0][2:4] == ("down", 2.9)


def test_cold_start_zero_threshold_emits_nothing():
    # CUSUM-Mid reports threshold 0.0 on bars that never reach S1 Step 3 (state/dwell gated),
    # where cusum_mid.update was never called — must not be read as a giant cross.
    e = _engine_stub()
    e._collect_cusum_cross("2026-07-11T12:00:00+00:00", "mid", 5.0, 0.0, 0.0)
    assert e._cusum_events == []


def test_below_threshold_emits_nothing():
    e = _engine_stub()
    e._collect_cusum_cross("2026-07-11T16:00:00+00:00", "short", 1.0, 1.0, 2.0)
    assert e._cusum_events == []


def test_cusum_events_accessor_returns_collected():
    e = _engine_stub()
    e._collect_cusum_cross("2026-07-11T00:00:00+00:00", "short", 2.5, 0.0, 2.0)
    assert e.cusum_events() is e._cusum_events
    assert len(e.cusum_events()) == 1


# ── DBWriter.write_cusum_events_bulk (recording fake conn — no live DB) ─────────


class _RecordingConn:
    def __init__(self):
        self.calls = []

    async def executemany(self, sql, args):
        self.calls.append((sql, args))


def test_write_cusum_events_bulk_encodes_and_upserts():
    w = DBWriter.__new__(DBWriter)
    conn = _RecordingConn()
    w._conn = conn
    import datetime as _dt

    ts = _dt.datetime(2026, 7, 11, tzinfo=_dt.timezone.utc)
    rows = [
        (ts, "short", "up", 2.5, 2.0, None),
        (ts, "mid", "down", 3.1, 1.69, [0.1, 0.2]),
    ]
    n = asyncio.run(w.write_cusum_events_bulk(rows))
    assert n == 2
    sql, args = conn.calls[0]
    assert "ON CONFLICT (timestamp, cusum_type) DO UPDATE" in sql
    # z_returns_window JSON-encoded (or None), rest passed straight through
    assert args[0][5] is None
    assert args[1][5] == "[0.1, 0.2]"


def test_write_cusum_events_bulk_empty_is_noop():
    w = DBWriter.__new__(DBWriter)
    conn = _RecordingConn()
    w._conn = conn
    assert asyncio.run(w.write_cusum_events_bulk([])) == 0
    assert conn.calls == []
