"""
Unit tests for sel_v2.scheduler.taker_flow_loader.

Tests:
- 4H aggregation: 1m rows within [bar_ts - 4H, bar_ts) are summed
- rows outside the window are excluded
- empty bar_timestamps → empty array
- no helixa rows → all NaN
- connection failure → all NaN
- time boundary: window is [bar_start, bar_end) — bar_start exclusive check
"""

import math
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sel_v2.scheduler.taker_flow_loader import load_taker_flow_series


def _ts(y, m, d, h=0, minute=0) -> datetime:
    return datetime(y, m, d, h, minute, tzinfo=timezone.utc)


def _mock_row(window_start: datetime, buy: float, sell: float) -> MagicMock:
    row = MagicMock()
    row.__getitem__ = lambda self, key: {
        "window_start": window_start,
        "taker_buy_volume": buy,
        "taker_sell_volume": sell,
    }[key]
    return row


def _make_conn(rows) -> AsyncMock:
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=rows)
    conn.close = AsyncMock()
    return conn


# ── 4H aggregation ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_single_bar_aggregates_1m_rows_in_window():
    # bar_ts = 2026-04-28 04:00 → window [00:00, 04:00)
    bar_ts = _ts(2026, 4, 28, 4)
    rows = [
        _mock_row(_ts(2026, 4, 28, 0, 0), buy=100.0, sell=80.0),  # net=+20
        _mock_row(_ts(2026, 4, 28, 1, 0), buy=200.0, sell=250.0),  # net=-50
        _mock_row(_ts(2026, 4, 28, 3, 59), buy=50.0, sell=30.0),  # net=+20
    ]
    mock_conn = _make_conn(rows)

    with patch("asyncpg.connect", AsyncMock(return_value=mock_conn)):
        result = await load_taker_flow_series([bar_ts])

    # Expected net: 20 - 50 + 20 = -10
    assert result["net_taker_flow"][0] == pytest.approx(-10.0)


@pytest.mark.asyncio
async def test_row_exactly_at_bar_ts_excluded():
    # window is [bar_ts - 4H, bar_ts) — bar_ts itself excluded
    bar_ts = _ts(2026, 4, 28, 4)
    rows = [
        _mock_row(_ts(2026, 4, 28, 4, 0), buy=999.0, sell=0.0),  # at bar_ts → outside
    ]
    mock_conn = _make_conn(rows)

    with patch("asyncpg.connect", AsyncMock(return_value=mock_conn)):
        result = await load_taker_flow_series([bar_ts])

    # searchsorted with side="left" for bar_ts means 04:00 is NOT included
    assert math.isnan(result["net_taker_flow"][0])


@pytest.mark.asyncio
async def test_row_exactly_at_bar_start_included():
    # window [bar_ts - 4H, bar_ts): bar_start = 00:00 should be included
    bar_ts = _ts(2026, 4, 28, 4)
    bar_start = _ts(2026, 4, 28, 0, 0)
    rows = [_mock_row(bar_start, buy=500.0, sell=300.0)]
    mock_conn = _make_conn(rows)

    with patch("asyncpg.connect", AsyncMock(return_value=mock_conn)):
        result = await load_taker_flow_series([bar_ts])

    assert result["net_taker_flow"][0] == pytest.approx(200.0)


@pytest.mark.asyncio
async def test_multiple_bars_independent_windows():
    bar1 = _ts(2026, 4, 28, 4)  # [00:00, 04:00)
    bar2 = _ts(2026, 4, 28, 8)  # [04:00, 08:00)

    rows = [
        _mock_row(_ts(2026, 4, 28, 0, 30), buy=100.0, sell=50.0),  # bar1 +50
        _mock_row(_ts(2026, 4, 28, 4, 0), buy=200.0, sell=180.0),  # bar2 +20
        _mock_row(_ts(2026, 4, 28, 7, 59), buy=80.0, sell=100.0),  # bar2 -20
    ]
    mock_conn = _make_conn(rows)

    with patch("asyncpg.connect", AsyncMock(return_value=mock_conn)):
        result = await load_taker_flow_series([bar1, bar2])

    assert result["net_taker_flow"][0] == pytest.approx(50.0)
    assert result["net_taker_flow"][1] == pytest.approx(0.0)  # 20 + (-20)


# ── Edge cases ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_bar_timestamps():
    mock_conn = _make_conn([])
    with patch("asyncpg.connect", AsyncMock(return_value=mock_conn)):
        result = await load_taker_flow_series([])
    assert result["net_taker_flow"].shape == (0,)


@pytest.mark.asyncio
async def test_no_rows_all_nan():
    bar_tss = [_ts(2026, 4, 28, 4), _ts(2026, 4, 28, 8)]
    mock_conn = _make_conn([])
    with patch("asyncpg.connect", AsyncMock(return_value=mock_conn)):
        result = await load_taker_flow_series(bar_tss)
    assert all(math.isnan(v) for v in result["net_taker_flow"])


@pytest.mark.asyncio
async def test_connection_failure_returns_all_nan():
    bar_tss = [_ts(2026, 4, 28, 4)]
    with patch(
        "asyncpg.connect",
        AsyncMock(side_effect=OSError("connection refused")),
    ):
        result = await load_taker_flow_series(bar_tss)
    assert math.isnan(result["net_taker_flow"][0])


@pytest.mark.asyncio
async def test_rows_outside_all_windows_yield_nan():
    # Only row is at 2026-04-25, bars are at 2026-04-28
    rows = [_mock_row(_ts(2026, 4, 25, 0), buy=100.0, sell=50.0)]
    bar_tss = [_ts(2026, 4, 28, 4), _ts(2026, 4, 28, 8)]
    mock_conn = _make_conn(rows)
    with patch("asyncpg.connect", AsyncMock(return_value=mock_conn)):
        result = await load_taker_flow_series(bar_tss)
    assert all(math.isnan(v) for v in result["net_taker_flow"])
