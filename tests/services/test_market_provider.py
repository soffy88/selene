"""Tests for services.data.ingestion.market_provider.MarketDataRESTClient.

Regression for the phantom data service: the gateway imported this module but it
did not exist, so /api/v4/backtest/wfo raised ImportError. These tests verify the
import resolves, the parsed rows match the shape WFOEngine.run() consumes, and the
backward pagination assembles a full ascending history without network access.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from services.data.ingestion.market_provider import (
    _INTERVAL_MS,
    MarketDataRESTClient,
    _parse_funding,
    _parse_klines,
)


def test_gateway_import_path_resolves():
    # The exact symbol the gateway imports.
    from services.data.ingestion.market_provider import MarketDataRESTClient as C

    assert C is MarketDataRESTClient


def test_parse_klines_shape_matches_engine():
    raw = [[1000, "10.0", "12.0", "9.0", "11.0", "100.0", 1999, "x", 5]]
    rows = _parse_klines(raw)
    assert rows == [
        {
            "open_time": 1000,
            "open": 10.0,
            "high": 12.0,
            "low": 9.0,
            "close": 11.0,
            "volume": 100.0,
        }
    ]
    # WFOEngine.run reads exactly these keys.
    for key in ("open_time", "open", "high", "low", "close", "volume"):
        assert key in rows[0]


def test_parse_funding_shape_and_types():
    raw = [{"symbol": "BTCUSDT", "fundingTime": 5000, "fundingRate": "0.0001"}]
    assert _parse_funding(raw) == [{"funding_time": 5000, "funding_rate": 0.0001}]


@pytest.mark.asyncio
async def test_fetch_klines_paginates_and_dedups():
    step = _INTERVAL_MS["1h"]
    t0 = 1_000_000_000_000
    total = 2000  # > _KLINES_MAX (1500) -> 2 pages
    history = [[t0 + i * step, "1", "2", "0.5", "1.5", "10"] for i in range(total)]

    async def fake_get_json(path, params):
        batch = params["limit"]
        end = params.get("endTime")
        eligible = [k for k in history if end is None or k[0] <= end]
        return eligible[-batch:]  # Binance returns ascending, oldest first

    client = MarketDataRESTClient()
    with patch.object(client, "_get_json", AsyncMock(side_effect=fake_get_json)):
        rows = await client.fetch_klines("BTCUSDT", "1h", limit=total)

    assert len(rows) == total
    times = [r["open_time"] for r in rows]
    assert times == sorted(times)  # ascending
    assert len(set(times)) == total  # no duplicates across page boundary


@pytest.mark.asyncio
async def test_fetch_klines_stops_at_history_start():
    step = _INTERVAL_MS["1h"]
    history = [[1000 + i * step, "1", "2", "0.5", "1.5", "10"] for i in range(50)]

    async def fake_get_json(path, params):
        batch = params["limit"]
        end = params.get("endTime")
        eligible = [k for k in history if end is None or k[0] <= end]
        return eligible[-batch:]

    client = MarketDataRESTClient()
    with patch.object(client, "_get_json", AsyncMock(side_effect=fake_get_json)):
        rows = await client.fetch_klines("BTCUSDT", "1h", limit=10_000)

    assert len(rows) == 50  # only 50 bars exist; no infinite loop


@pytest.mark.asyncio
async def test_fetch_funding_sorted_ascending():
    raw = [
        {"symbol": "BTCUSDT", "fundingTime": 3000, "fundingRate": "0.0003"},
        {"symbol": "BTCUSDT", "fundingTime": 1000, "fundingRate": "0.0001"},
    ]
    client = MarketDataRESTClient()
    with patch.object(client, "_get_json", AsyncMock(return_value=raw)):
        rows = await client.fetch_funding_history("BTCUSDT")
    assert [r["funding_time"] for r in rows] == [1000, 3000]


def test_unsupported_interval_raises():
    client = MarketDataRESTClient()
    with pytest.raises(ValueError):
        # sync raise happens before any await in fetch_klines
        import asyncio

        asyncio.run(client.fetch_klines("BTCUSDT", "7h", limit=10))
