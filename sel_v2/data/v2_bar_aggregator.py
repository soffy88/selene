import asyncio
import logging
import os
from datetime import datetime, timezone, timedelta
import asyncpg

from sel_v2.db.migrations import apply_schema

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("v2_bar_aggregator")

DB_URL = os.environ.get("DB_URL")
SYMBOL = os.environ.get("SYMBOLS", "BTC-USDT")
# REST gap-fill uses the perp candle (matches the tick feed instrument, see P0-2).
INST_ID = os.environ.get("BAR_INST_ID", f"{SYMBOL}-SWAP")
BASE_URL = "https://www.okx.com"

def get_4h_boundaries(ts: datetime):
    # Align to 4H: 0, 4, 8, 12, 16, 20
    hour = (ts.hour // 4) * 4
    start = ts.replace(hour=hour, minute=0, second=0, microsecond=0)
    end = start + timedelta(hours=4)
    return start, end


def build_bar_row(ticks, start: datetime, symbol: str):
    """Build a v2_bars_4h row tuple from this bar's ticks, or None when there were none.
    Pure (no I/O) so the aggregation is unit-testable."""
    if not ticks:
        return None
    prices = [float(t["price"]) for t in ticks]
    sizes = [float(t["size"]) for t in ticks]
    volume = sum(sizes)
    # NULL (unknown) when there's no volume to weight by, not a fake 0.0 (P2-5).
    vwap = (sum(p * s for p, s in zip(prices, sizes)) / volume) if volume > 0 else None
    return (start, symbol, prices[0], max(prices), min(prices), prices[-1],
            volume, vwap, len(ticks))


def parse_rest_candle(candles, start: datetime, symbol: str):
    """Find the OKX history-candle whose open time == bar start and map it to a row tuple,
    or None if absent. tick_count=0 marks the bar as REST-recovered (a gap fill), not a hole.
    Pure so it's unit-testable without a network call."""
    start_ms = int(start.timestamp() * 1000)
    for c in candles or []:
        if int(c[0]) == start_ms:
            o, h, l, cl, vol = float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])
            # vwap NULL (REST candles carry no VWAP), tick_count=0 marks REST-recovered (P2-5).
            return (start, symbol, o, h, l, cl, vol, None, 0)
    return None


async def _fetch_rest_bar_row(session, start: datetime, symbol: str):
    """Fetch the official OKX 4H candle for the bar at `start` to recover a tick gap.
    Returns a row tuple or None. Never raises (gap recovery is best-effort)."""
    end_ms = int((start + timedelta(hours=4)).timestamp() * 1000)
    url = (f"{BASE_URL}/api/v5/market/history-candles"
           f"?instId={INST_ID}&bar=4H&after={end_ms}&limit=3")
    try:
        async with session.get(url, timeout=10) as resp:
            data = await resp.json()
    except Exception as e:
        logger.warning(f"gap-fill REST fetch failed for ts={start}: {e}")
        return None
    if data.get("code") != "0":
        return None
    return parse_rest_candle(data.get("data"), start, symbol)


async def aggregate_bar(pool, start: datetime, end: datetime, session=None):
    async with pool.acquire() as conn:
        ticks = await conn.fetch(
            "SELECT price, size FROM v2_ticks WHERE symbol=$1 AND timestamp >= $2 AND timestamp < $3 ORDER BY timestamp ASC",
            SYMBOL, start, end
        )
        row = build_bar_row(ticks, start, SYMBOL)
        recovered = False
        if row is None:
            # Tick gap (e.g. a WS outage lost this bar's trades). Don't leave a hole — recover
            # the bar from the official OKX candle so the 4H series stays contiguous (P1-4).
            if session is not None:
                row = await _fetch_rest_bar_row(session, start, SYMBOL)
                recovered = row is not None
            if row is None:
                logger.warning(f"GAP: no ticks and no REST recovery for bar ts={start} — "
                               f"bar missing (downstream rolling windows will see a hole)")
                return

        await conn.execute(
            """INSERT INTO v2_bars_4h (time, symbol, open, high, low, close, volume, vwap, tick_count)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
               ON CONFLICT (time, symbol) DO NOTHING""",
            *row
        )
        src = "REST-recovered" if recovered else "ticks"
        logger.info(f"INSERT bar time={start} O={row[2]} H={row[3]} L={row[4]} C={row[5]} "
                    f"vol={row[6]} tick_count={row[8]} src={src}")

async def main():
    import aiohttp
    pool = await asyncpg.create_pool(DB_URL)
    await apply_schema(pool)

    # Check max bar
    async with pool.acquire() as conn:
        max_bar = await conn.fetchval("SELECT MAX(time) FROM v2_bars_4h WHERE symbol=$1", SYMBOL)

    now = datetime.now(timezone.utc)
    current_start, _ = get_4h_boundaries(now)

    async with aiohttp.ClientSession(trust_env=True) as session:
        if max_bar:
            # backfill from max_bar to current_start (REST-recover any tick gaps)
            next_bar = max_bar + timedelta(hours=4)
            while next_bar < current_start:
                await aggregate_bar(pool, next_bar, next_bar + timedelta(hours=4), session=session)
                next_bar += timedelta(hours=4)

        while True:
            now = datetime.now(timezone.utc)
            current_start, current_end = get_4h_boundaries(now)
            # We wait until current_end + 120s
            wait_until = current_end + timedelta(seconds=120)
            sleep_sec = (wait_until - now).total_seconds()

            if sleep_sec > 0:
                await asyncio.sleep(sleep_sec)

            # Now aggregate the bar that just finished (REST-recover if ticks are missing)
            await aggregate_bar(pool, current_start, current_end, session=session)

if __name__ == "__main__":
    asyncio.run(main())
