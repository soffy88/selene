"""
Binance USD-M Futures REST market-data client for on-demand backtests.

This is the provider the gateway's /api/v4/backtest/wfo endpoint expects
(`services.data.ingestion.market_provider.MarketDataRESTClient`). It was imported
but never existed — the WFO endpoint raised ImportError on every call. It returns
candles and funding history in exactly the shape WFOEngine.run() consumes:

  candles: list[dict]  with keys open_time, open, high, low, close, volume
  funding: list[dict]  with keys funding_time, funding_rate

Network I/O is isolated in `_get_json`; the row-shaping logic (`_parse_klines`,
`_parse_funding`) is pure and unit-tested.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Optional

import aiohttp

BINANCE_FAPI = os.getenv("BINANCE_FAPI_BASE", "https://fapi.binance.com")

# Binance kline limit per request, and funding-rate limit per request.
_KLINES_MAX = 1500
_FUNDING_MAX = 1000

# interval string -> milliseconds, for backward pagination.
_INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "6h": 21_600_000,
    "8h": 28_800_000, "12h": 43_200_000, "1d": 86_400_000,
}


def _parse_klines(raw: list[list]) -> list[dict]:
    """Map Binance kline arrays to the dict shape WFOEngine.run expects.
    Binance row: [openTime, open, high, low, close, volume, closeTime, ...]."""
    out = []
    for k in raw:
        out.append({
            "open_time": int(k[0]),
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
        })
    return out


def _parse_funding(raw: list[dict]) -> list[dict]:
    """Map Binance fundingRate records to {funding_time, funding_rate}."""
    return [
        {"funding_time": int(r["fundingTime"]), "funding_rate": float(r["fundingRate"])}
        for r in raw
    ]


class MarketDataRESTClient:
    def __init__(self, base_url: str = BINANCE_FAPI, timeout_s: float = 20.0) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout_s)

    async def _get_json(self, path: str, params: dict) -> Any:
        async with aiohttp.ClientSession(timeout=self._timeout) as session:
            async with session.get(self._base + path, params=params) as resp:
                resp.raise_for_status()
                return await resp.json()

    async def fetch_klines(
        self, symbol: str, interval: str = "1h", limit: int = 1000,
    ) -> list[dict]:
        """Fetch up to `limit` most-recent klines, paging backward when
        limit > the per-request cap. Returned ascending by open_time."""
        step = _INTERVAL_MS.get(interval)
        if step is None:
            raise ValueError(f"unsupported interval {interval!r}")

        remaining = max(0, int(limit))
        collected: dict[int, dict] = {}
        end_time: Optional[int] = None

        while remaining > 0:
            batch = min(remaining, _KLINES_MAX)
            params = {"symbol": symbol, "interval": interval, "limit": batch}
            if end_time is not None:
                params["endTime"] = end_time
            raw = await self._get_json("/fapi/v1/klines", params)
            rows = _parse_klines(raw)
            if not rows:
                break
            for r in rows:
                collected[r["open_time"]] = r
            oldest = rows[0]["open_time"]
            remaining = limit - len(collected)
            if oldest <= 0 or len(rows) < batch:
                break  # reached the start of available history
            end_time = oldest - 1
            await asyncio.sleep(0)  # cooperative yield between pages

        return [collected[t] for t in sorted(collected)]

    async def fetch_funding_history(
        self, symbol: str, limit: int = _FUNDING_MAX,
    ) -> list[dict]:
        """Fetch funding-rate history (most recent `limit` settlements),
        ascending by funding_time."""
        params = {"symbol": symbol, "limit": min(int(limit), _FUNDING_MAX)}
        raw = await self._get_json("/fapi/v1/fundingRate", params)
        rows = _parse_funding(raw)
        rows.sort(key=lambda r: r["funding_time"])
        return rows
