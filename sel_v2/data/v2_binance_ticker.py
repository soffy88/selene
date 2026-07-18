"""Binance spot ticker poller (Wave S2C Part 3) — a second reachable price for the
Strategy-2 Step-5 cross-exchange divergence check.

Context: OKX is globally unreachable in this deployment and the local price reference is
already Binance-perp-derived (v2_bars_4h backfilled from Binance fapi). So a Binance-vs-
Binance mid compare would be degenerate. Instead we poll Binance *spot* (api.binance.com):
the perp-vs-spot basis is a real, reachable cross-market dislocation signal (a basis blowout
front-runs cascades). Stored as exchange='binance_spot' in v2_cross_exchange_prices; Step 5
compares it to the local perp mark.

Proxy: same known-good helios-proxy:2080 the other Binance collectors use (BINANCE_PROXY),
NOT the container HTTPS_PROXY (which still carries the dead OKX proxy). The proxy is flaky on
long connections but fine for independent 30s polls with retries — same rationale as
binance_rest.py. If the proxy blocks the spot host, each cycle logs and skips (Step 5 then
degrades to data-unavailable and does not abort — see strategy2_entry Step 5).
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

import aiohttp
import asyncpg

from sel_v2.data.binance_rest import BINANCE_PROXY, to_binance_symbol
from sel_v2.db.migrations import apply_schema

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("v2_binance_ticker")

DB_URL = os.environ.get("DB_URL")
BASE_SYMBOL = os.environ.get("SYMBOLS", "BTC-USDT")
FETCH_SYMBOL = to_binance_symbol(BASE_SYMBOL)  # 'BTCUSDT'
# Spot REST base (the endpoint the spec names). Separate from fapi used by the perp collectors.
BINANCE_SPOT_BASE = os.environ.get("BINANCE_SPOT_BASE", "https://api.binance.com")
POLL_SECONDS = int(os.environ.get("BINANCE_TICKER_POLL_SECONDS", "30"))
EXCHANGE_LABEL = "binance_spot"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)


async def fetch_spot_price(session, symbol: str, *, retries: int = 3):
    """GET /api/v3/ticker/price?symbol= through BINANCE_PROXY → float price, or None.
    Retries a few times with short backoff (the proxy resets TLS intermittently even on
    REST); a single dropped request must not cost the whole poll cycle."""
    url = f"{BINANCE_SPOT_BASE}/api/v3/ticker/price"
    for attempt in range(retries):
        try:
            async with session.get(
                url, params={"symbol": symbol}, proxy=BINANCE_PROXY
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                p = data.get("price") if isinstance(data, dict) else None
                return float(p) if p not in (None, "") else None
        except Exception:  # transient proxy TLS reset / timeout
            await asyncio.sleep(0.5 * (attempt + 1))
    return None


async def main():
    pool = await asyncpg.create_pool(DB_URL)
    await apply_schema(pool)
    logger.info(
        "binance ticker poller: spot %s via %s every %ss (stored exchange=%s)",
        FETCH_SYMBOL,
        BINANCE_PROXY,
        POLL_SECONDS,
        EXCHANGE_LABEL,
    )
    inserts = 0
    async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
        while True:
            try:
                price = await fetch_spot_price(session, FETCH_SYMBOL)
                if price is not None and price > 0:
                    ts = datetime.now(timezone.utc)
                    await pool.execute(
                        "INSERT INTO v2_cross_exchange_prices (timestamp, exchange, price) "
                        "VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
                        ts,
                        EXCHANGE_LABEL,
                        price,
                    )
                    inserts += 1
                    if inserts == 1 or inserts % 20 == 0:
                        logger.info(
                            "v2_cross_exchange_prices inserts=%s (last %s=%.2f)",
                            inserts,
                            EXCHANGE_LABEL,
                            price,
                        )
                else:
                    logger.warning("binance spot price unavailable this cycle (proxy?)")
            except Exception as e:  # noqa: BLE001
                logger.error("binance ticker poll error: %s", e)
            await asyncio.sleep(POLL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
