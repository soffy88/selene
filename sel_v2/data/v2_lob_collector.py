import asyncio
import json
import logging
import os
import math
from datetime import datetime, timezone
import asyncpg
from websockets_proxy import proxy_connect, Proxy

from sel_v2.db.migrations import apply_schema

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("v2_lob_collector")

DB_URL = os.environ.get("DB_URL")
SYMBOL = os.environ.get("SYMBOLS", "BTC-USDT")
PROXY_URL = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")

def calc_entropy(bids, asks):
    sizes = [float(b[1]) for b in bids[:5]] + [float(a[1]) for a in asks[:5]]
    total = sum(sizes)
    if total == 0: return 0.0
    probs = [s / total for s in sizes if s > 0]
    return -sum(p * math.log2(p) for p in probs)

async def collect_lob(pool):
    insert_count = 0
    proxy = Proxy.from_url(PROXY_URL) if PROXY_URL else None
    
    async with proxy_connect(
        "wss://ws.okx.com:8443/ws/v5/public",
        proxy=proxy
    ) as ws:
        await ws.send(json.dumps({
            "op": "subscribe",
            "args": [{"channel": "books5", "instId": SYMBOL}]
        }))
        logger.info(f"Subscribed to books5 for {SYMBOL} via proxy: {PROXY_URL}")
        
        async for msg in ws:
            data = json.loads(msg)
            if "data" in data:
                for item in data["data"]:
                    ts = datetime.fromtimestamp(int(item["ts"])/1000, tz=timezone.utc)
                    bids = item.get("bids", [])
                    asks = item.get("asks", [])
                    
                    bid_depth = sum(float(b[1]) for b in bids[:5])
                    ask_depth = sum(float(a[1]) for a in asks[:5])
                    entropy = calc_entropy(bids, asks)
                    
                    try:
                        await pool.execute(
                            "INSERT INTO v2_lob_snapshots (timestamp, symbol, bids, asks, bid_depth, ask_depth, entropy) VALUES ($1, $2, $3, $4, $5, $6, $7)",
                            ts, SYMBOL, json.dumps(bids), json.dumps(asks), bid_depth, ask_depth, entropy
                        )
                        insert_count += 1
                        if insert_count == 1:
                            logger.info("INSERT row 1 to v2_lob_snapshots")
                        if insert_count % 100 == 0:
                            logger.info(f"v2_lob_snapshots cumulative inserts: {insert_count}")
                    except Exception as db_e:
                        logger.error(f"INSERT failed: {db_e}")

async def main():
    pool = await asyncpg.create_pool(DB_URL)
    await apply_schema(pool)
    retry_delay = 1
    while True:
        try:
            await collect_lob(pool)
            retry_delay = 1
        except Exception as e:
            logger.error(f"Connection error: {e}. Retrying in {retry_delay}s...")
            await asyncio.sleep(retry_delay)
            retry_delay = min(60, retry_delay * 2)

if __name__ == "__main__":
    asyncio.run(main())
