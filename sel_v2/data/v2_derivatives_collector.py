import asyncio
import logging
import os
from datetime import datetime, timezone
import aiohttp
import asyncpg

from sel_v2.db.migrations import apply_schema

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("v2_derivatives_collector")

DB_URL = os.environ.get("DB_URL")
SYMBOL = "BTC-USDT-SWAP"
BASE_URL = "https://www.okx.com"

async def fetch_data(session, path):
    async with session.get(f"{BASE_URL}{path}?instId={SYMBOL}") as response:
        data = await response.json()
        if data.get("code") == "0" and data.get("data"):
            return data["data"][0]
        return None

async def main():
    pool = await asyncpg.create_pool(DB_URL)
    await apply_schema(pool)
    insert_count = 0
    
    async with aiohttp.ClientSession(trust_env=True) as session:
        while True:
            try:
                funding_data = await fetch_data(session, "/api/v5/public/funding-rate")
                oi_data = await fetch_data(session, "/api/v5/public/open-interest")
                mark_data = await fetch_data(session, "/api/v5/public/mark-price")
                
                if funding_data and oi_data:
                    # Sometimes OKX time is slightly off, we use max timestamp among returned data or just current time
                    ts_val = max(int(oi_data.get("ts", 0)), int(funding_data.get("ts", 0)))
                    if ts_val == 0:
                        ts_val = int(datetime.now(timezone.utc).timestamp() * 1000)
                    ts = datetime.fromtimestamp(ts_val/1000, tz=timezone.utc)
                    
                    funding_rate = float(funding_data.get("fundingRate", 0))
                    open_interest = float(oi_data.get("oi", 0))
                    mark_price = float(mark_data.get("markPx", 0)) if mark_data else 0.0
                    index_price = float(mark_data.get("idxPx", 0)) if mark_data else 0.0
                    
                    try:
                        await pool.execute(
                            "INSERT INTO v2_derivatives_snapshots (timestamp, symbol, funding_rate, open_interest, mark_price, index_price) VALUES ($1, $2, $3, $4, $5, $6)",
                            ts, "BTC-USDT", funding_rate, open_interest, mark_price, index_price
                        )
                        insert_count += 1
                        if insert_count == 1:
                            logger.info("INSERT row 1 to v2_derivatives_snapshots")
                        if insert_count % 10 == 0:
                            logger.info(f"v2_derivatives_snapshots cumulative inserts: {insert_count}")
                    except Exception as db_e:
                        logger.error(f"INSERT failed: {db_e}")
            except Exception as e:
                logger.error(f"Error fetching derivatives: {e}")
            
            await asyncio.sleep(30)

if __name__ == "__main__":
    asyncio.run(main())
