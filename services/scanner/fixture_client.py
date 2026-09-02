"""Redis-backed market-data client for isolated qualification. No venue HTTP."""

from __future__ import annotations

import json
from typing import Optional

from shared.db.redis_client import get_redis

TICKERS_KEY = "qual:tickers"
KLINES_KEY = "qual:klines:{symbol}"
FUNDING_KEY = "qual:funding:{symbol}"
FUNDING_NOW_KEY = "qual:funding_now:{symbol}"
OI_KEY = "qual:oi_change:{symbol}"
LSR_KEY = "qual:long_ratio:{symbol}"


def _decode(raw):
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode()
    return json.loads(raw)


class FixtureScanClient:
    """Same surface as BinanceScanClient; reads deterministic Redis fixtures only."""

    async def ticker_24h(self) -> list[dict]:
        r = get_redis()
        data = _decode(await r.get(TICKERS_KEY))
        return data or []

    async def klines(self, symbol: str, limit: int) -> list[dict]:
        r = get_redis()
        data = _decode(await r.get(KLINES_KEY.format(symbol=symbol))) or []
        return data[-limit:]

    async def funding_history(self, symbol: str, limit: int = 30) -> list[float]:
        r = get_redis()
        data = _decode(await r.get(FUNDING_KEY.format(symbol=symbol))) or []
        return [float(x) for x in data][-limit:]

    async def funding_rate_now(self, symbol: str) -> Optional[float]:
        r = get_redis()
        raw = await r.get(FUNDING_NOW_KEY.format(symbol=symbol))
        if raw is None:
            hist = await self.funding_history(symbol, 1)
            return hist[-1] if hist else None
        text = raw.decode() if isinstance(raw, bytes) else raw
        return float(text)

    async def oi_change_pct(self, symbol: str) -> Optional[float]:
        r = get_redis()
        raw = await r.get(OI_KEY.format(symbol=symbol))
        if raw is None:
            return 4.0
        text = raw.decode() if isinstance(raw, bytes) else raw
        return float(text)

    async def long_ratio_pct(self, symbol: str) -> Optional[float]:
        r = get_redis()
        raw = await r.get(LSR_KEY.format(symbol=symbol))
        if raw is None:
            return 32.0
        text = raw.decode() if isinstance(raw, bytes) else raw
        return float(text)
