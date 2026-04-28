"""
Back-fill BTC 1H OHLCV candles from OKX history-candles API.

OKX endpoint: GET /api/v5/market/history-candles
  ?instId=BTC-USDT-SWAP&bar=1H&limit=100&after={ts_ms}

Paginates backwards from `end_ts` until `start_ts` is reached or no more data.
Upserts rows into the existing `candles` hypertable.
"""
import asyncio
import logging
import time
from datetime import datetime, timezone

import aiohttp
import asyncpg

logger = logging.getLogger(__name__)

OKX_HISTORY_URL = "https://www.okx.com/api/v5/market/history-candles"
PAGE_SIZE = 100
REQUEST_DELAY = 0.3  # seconds between requests to avoid rate-limiting

_UPSERT_CANDLE_SQL = """
INSERT INTO candles (symbol, interval, open_time, open, high, low, close, volume, close_time)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
ON CONFLICT (symbol, interval, open_time) DO NOTHING
"""


async def _fetch_page(
    session: aiohttp.ClientSession,
    inst_id: str,
    after_ms: int | None,
) -> list[list]:
    params = {"instId": inst_id, "bar": "1H", "limit": str(PAGE_SIZE)}
    if after_ms is not None:
        params["after"] = str(after_ms)

    for attempt in range(3):
        try:
            async with session.get(
                OKX_HISTORY_URL,
                params=params,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status == 429:
                    await asyncio.sleep(2 ** attempt * 2)
                    continue
                if resp.status != 200:
                    logger.warning("history-candles HTTP %s", resp.status)
                    return []
                data = await resp.json()
                return (data or {}).get("data", [])
        except Exception as exc:
            logger.warning("history-candles fetch attempt %d error: %s", attempt + 1, exc)
            await asyncio.sleep(1)
    return []


async def backfill_candles(
    pool: asyncpg.Pool,
    symbol: str = "BTCUSDT",
    inst_id: str = "BTC-USDT-SWAP",
    start_ts: datetime | None = None,
    end_ts: datetime | None = None,
) -> int:
    """
    Backfill 1H candles for `symbol` from OKX.
    Returns count of rows inserted.
    start_ts defaults to 2 years ago; end_ts defaults to now.
    """
    if end_ts is None:
        end_ts = datetime.now(timezone.utc)
    if start_ts is None:
        from datetime import timedelta
        start_ts = end_ts - timedelta(days=730)  # 2 years

    start_ms = int(start_ts.timestamp() * 1000)
    after_ms: int | None = int(end_ts.timestamp() * 1000)

    total_inserted = 0
    async with aiohttp.ClientSession() as session:
        while True:
            rows = await _fetch_page(session, inst_id, after_ms)
            if not rows:
                logger.info("backfill complete for %s: %d rows inserted", symbol, total_inserted)
                break

            # OKX returns newest-first; each row: [ts_ms, o, h, l, c, vol, volCcy, ?, confirm]
            batch = []
            oldest_ts_ms = None
            for item in rows:
                ts_ms = int(item[0])
                if ts_ms < start_ms:
                    continue
                oldest_ts_ms = ts_ms
                open_time = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
                close_time = datetime.fromtimestamp((ts_ms + 3_600_000) / 1000, tz=timezone.utc)
                batch.append((
                    symbol, "1h", open_time,
                    float(item[1]), float(item[2]), float(item[3]), float(item[4]),
                    float(item[5]),
                    close_time,
                ))

            if batch:
                async with pool.acquire() as conn:
                    result = await conn.executemany(_UPSERT_CANDLE_SQL, batch)
                count = len(batch)
                total_inserted += count
                logger.info(
                    "backfill %s: inserted %d rows (oldest=%s)",
                    symbol, count,
                    datetime.fromtimestamp(oldest_ts_ms / 1000, tz=timezone.utc).isoformat() if oldest_ts_ms else "?",
                )

            # Stop if we've reached further back than start_ts
            if oldest_ts_ms is None or oldest_ts_ms <= start_ms:
                break

            # Paginate: OKX `after` = timestamp of the oldest bar in last page
            after_ms = int(rows[-1][0])
            await asyncio.sleep(REQUEST_DELAY)

    return total_inserted


async def run_backfill(
    symbol: str = "BTCUSDT",
    inst_id: str = "BTC-USDT-SWAP",
    years: int = 2,
) -> None:
    from shared.db.connections import get_pg
    pool = await get_pg()
    try:
        n = await backfill_candles(pool, symbol, inst_id)
        logger.info("backfill done: %d rows", n)
    finally:
        await pool.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_backfill())
