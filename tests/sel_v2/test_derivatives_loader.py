"""
Unit tests for sel_v2.scheduler.derivatives_loader.

Tests:
- asof join: each bar_ts gets the latest snapshot at or before it
- empty bar_timestamps → empty arrays
- no helixa rows → all NaN
- partial coverage → NaN before first snapshot, values after
- connection failure → all NaN (no exception propagated)
"""

import math
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sel_v2.scheduler.derivatives_loader import load_oi_funding_series


def _ts(y, m, d, h=0) -> datetime:
    return datetime(y, m, d, h, tzinfo=timezone.utc)


def _mock_row(snapshot_ts: datetime, oi: float, fr: float) -> MagicMock:
    """Build a mock asyncpg Record-like object."""
    row = MagicMock()
    row.__getitem__ = lambda self, key: {
        "snapshot_ts": snapshot_ts,
        "open_interest": oi,
        "funding_rate": fr,
    }[key]
    row.get = lambda key, default=None: {
        "snapshot_ts": snapshot_ts,
        "open_interest": oi,
        "funding_rate": fr,
    }.get(key, default)
    # asyncpg record has .tzinfo on the stored datetime
    return row


def _make_conn(rows) -> AsyncMock:
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=rows)
    conn.close = AsyncMock()
    return conn


# ── Basic asof join ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_asof_join_exact_match():
    snap_ts = _ts(2026, 4, 27, 8)
    bar_ts = _ts(2026, 4, 27, 8)  # exact match

    row = _mock_row(snap_ts, oi=1_000_000.0, fr=0.0001)
    mock_conn = _make_conn([row])

    with patch("asyncpg.connect", AsyncMock(return_value=mock_conn)):
        result = await load_oi_funding_series([bar_ts], symbol="BTC")

    assert not math.isnan(result["open_interest"][0])
    assert result["open_interest"][0] == pytest.approx(1_000_000.0)
    assert result["funding_rate"][0] == pytest.approx(0.0001)


@pytest.mark.asyncio
async def test_asof_join_snapshot_before_bar():
    snap_ts = _ts(2026, 4, 27, 4)
    bar_ts = _ts(2026, 4, 27, 8)  # snapshot 4h earlier → should fill

    row = _mock_row(snap_ts, oi=2_500_000.0, fr=-0.0003)
    mock_conn = _make_conn([row])

    with patch("asyncpg.connect", AsyncMock(return_value=mock_conn)):
        result = await load_oi_funding_series([bar_ts], symbol="BTC")

    assert result["open_interest"][0] == pytest.approx(2_500_000.0)
    assert result["funding_rate"][0] == pytest.approx(-0.0003)


@pytest.mark.asyncio
async def test_asof_join_snapshot_after_bar_yields_nan():
    snap_ts = _ts(2026, 4, 27, 12)  # snapshot AFTER bar
    bar_ts = _ts(2026, 4, 27, 8)

    row = _mock_row(snap_ts, oi=999.0, fr=0.01)
    mock_conn = _make_conn([row])

    with patch("asyncpg.connect", AsyncMock(return_value=mock_conn)):
        result = await load_oi_funding_series([bar_ts], symbol="BTC")

    assert math.isnan(result["open_interest"][0])
    assert math.isnan(result["funding_rate"][0])


@pytest.mark.asyncio
async def test_asof_join_multiple_bars_partial_coverage():
    snap1 = _mock_row(_ts(2026, 4, 27, 8), oi=1e6, fr=0.0001)
    snap2 = _mock_row(_ts(2026, 4, 27, 16), oi=1.1e6, fr=0.0002)

    bar_tss = [
        _ts(2026, 4, 27, 4),  # before any snapshot → NaN
        _ts(2026, 4, 27, 8),  # exact match snap1
        _ts(2026, 4, 27, 12),  # between snap1 and snap2 → snap1
        _ts(2026, 4, 27, 20),  # after snap2 → snap2
    ]
    mock_conn = _make_conn([snap1, snap2])

    with patch("asyncpg.connect", AsyncMock(return_value=mock_conn)):
        result = await load_oi_funding_series(bar_tss, symbol="BTC")

    oi = result["open_interest"]
    assert math.isnan(oi[0])
    assert oi[1] == pytest.approx(1e6)
    assert oi[2] == pytest.approx(1e6)  # asof = snap1
    assert oi[3] == pytest.approx(1.1e6)  # asof = snap2


# ── Edge cases ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_bar_timestamps():
    mock_conn = _make_conn([])
    with patch("asyncpg.connect", AsyncMock(return_value=mock_conn)):
        result = await load_oi_funding_series([], symbol="BTC")
    assert result["open_interest"].shape == (0,)
    assert result["funding_rate"].shape == (0,)


@pytest.mark.asyncio
async def test_no_helixa_rows_all_nan():
    bar_tss = [_ts(2026, 4, 27, 8), _ts(2026, 4, 27, 12)]
    mock_conn = _make_conn([])
    with patch("asyncpg.connect", AsyncMock(return_value=mock_conn)):
        result = await load_oi_funding_series(bar_tss, symbol="BTC")
    assert all(math.isnan(v) for v in result["open_interest"])
    assert all(math.isnan(v) for v in result["funding_rate"])


@pytest.mark.asyncio
async def test_connection_failure_returns_all_nan():
    bar_tss = [_ts(2026, 4, 27, 8)]
    with patch(
        "asyncpg.connect",
        AsyncMock(side_effect=ConnectionRefusedError("refused")),
    ):
        result = await load_oi_funding_series(bar_tss, symbol="BTC")
    assert math.isnan(result["open_interest"][0])
    assert math.isnan(result["funding_rate"][0])


@pytest.mark.asyncio
async def test_null_oi_in_row_becomes_nan():
    snap = _mock_row(_ts(2026, 4, 27, 8), oi=None, fr=0.0001)  # type: ignore[arg-type]
    bar_ts = _ts(2026, 4, 27, 8)
    mock_conn = _make_conn([snap])
    with patch("asyncpg.connect", AsyncMock(return_value=mock_conn)):
        result = await load_oi_funding_series([bar_ts], symbol="BTC")
    # oi None → float(None) raises; the loader guards with 'if r["open_interest"] is not None'
    assert math.isnan(result["open_interest"][0])
