import asyncio
import logging
import os
from datetime import datetime, timezone, timedelta
import asyncpg

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("v2_bar_aggregator")

DB_URL = os.environ.get("DB_URL")
SYMBOL = os.environ.get("SYMBOLS", "BTC-USDT")

def get_4h_boundaries(ts: datetime):
    # Align to 4H: 0, 4, 8, 12, 16, 20
    hour = (ts.hour // 4) * 4
    start = ts.replace(hour=hour, minute=0, second=0, microsecond=0)
    end = start + timedelta(hours=4)
    return start, end

async def aggregate_bar(pool, start: datetime, end: datetime):
    async with pool.acquire() as conn:
        ticks = await conn.fetch(
            "SELECT price, size FROM v2_ticks WHERE symbol=$1 AND timestamp >= $2 AND timestamp < $3 ORDER BY timestamp ASC",
            SYMBOL, start, end
        )
        if not ticks:
            logger.warning(f"no ticks for bar ts={start}, skipped")
            return

        open_p = float(ticks[0]["price"])
        close_p = float(ticks[-1]["price"])
        high_p = max(float(t["price"]) for t in ticks)
        low_p = min(float(t["price"]) for t in ticks)
        volume = sum(float(t["size"]) for t in ticks)
        
        vwap = 0.0
        if volume > 0:
            vwap = sum(float(t["price"]) * float(t["size"]) for t in ticks) / volume
            
        tick_count = len(ticks)

        await conn.execute(
            """INSERT INTO v2_bars_4h (time, symbol, open, high, low, close, volume, vwap, tick_count) 
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) 
               ON CONFLICT (time, symbol) DO NOTHING""",
            start, SYMBOL, open_p, high_p, low_p, close_p, volume, vwap, tick_count
        )
        logger.info(f"INSERT bar time={start} O={open_p} H={high_p} L={low_p} C={close_p} vol={volume} tick_count={tick_count}")

async def main():
    pool = await asyncpg.create_pool(DB_URL)
    
    # Check max bar
    async with pool.acquire() as conn:
        max_bar = await conn.fetchval("SELECT MAX(time) FROM v2_bars_4h WHERE symbol=$1", SYMBOL)
    
    now = datetime.now(timezone.utc)
    current_start, _ = get_4h_boundaries(now)
    
    if max_bar:
        # backfill from max_bar to current_start
        next_bar = max_bar + timedelta(hours=4)
        while next_bar < current_start:
            await aggregate_bar(pool, next_bar, next_bar + timedelta(hours=4))
            next_bar += timedelta(hours=4)
            
    while True:
        now = datetime.now(timezone.utc)
        current_start, current_end = get_4h_boundaries(now)
        # We wait until current_end + 120s
        wait_until = current_end + timedelta(seconds=120)
        sleep_sec = (wait_until - now).total_seconds()
        
        if sleep_sec > 0:
            await asyncio.sleep(sleep_sec)
        
        # Now aggregate the bar that just finished
        await aggregate_bar(pool, current_start, current_end)

if __name__ == "__main__":
    asyncio.run(main())
