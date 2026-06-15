import asyncio
import json
import logging
import os
from datetime import datetime, timezone
import asyncpg
from websockets_proxy import proxy_connect, Proxy

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("v2_liquidation_collector")

DB_URL = os.environ.get("DB_URL")
SYMBOL = "BTC-USDT-SWAP"
PROXY_URL = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")

async def collect_liquidations(pool):
    insert_count = 0
    proxy = Proxy.from_url(PROXY_URL) if PROXY_URL else None
    
    async with proxy_connect(
        "wss://ws.okx.com:8443/ws/v5/public",
        proxy=proxy
    ) as ws:
        await ws.send(json.dumps({
            "op": "subscribe",
            "args": [{"channel": "liquidation-orders", "instType": "SWAP"}]
        }))
        logger.info(f"Subscribed to liquidation-orders via proxy: {PROXY_URL}")
        
        async for msg in ws:
            data = json.loads(msg)
            if "data" in data:
                for item in data["data"]:
                    for detail in item.get("details", []):
                        if detail.get("instId") != SYMBOL:
                            continue
                        ts = datetime.fromtimestamp(int(detail["ts"])/1000, tz=timezone.utc)
                        side = detail["side"]
                        size = float(detail["sz"])
                        price = float(detail["bkPx"]) if detail.get("bkPx") else 0.0
                        loss = float(detail["bkLoss"]) if detail.get("bkLoss") else 0.0
                        
                        try:
                            await pool.execute(
                                "INSERT INTO v2_liquidations (timestamp, symbol, side, size, price, loss) VALUES ($1, $2, $3, $4, $5, $6)",
                                ts, "BTC-USDT", side, size, price, loss
                            )
                            insert_count += 1
                            if insert_count == 1:
                                logger.info("INSERT row 1 to v2_liquidations")
                            if insert_count % 10 == 0:
                                logger.info(f"v2_liquidations cumulative inserts: {insert_count}")
                        except Exception as db_e:
                            logger.error(f"INSERT failed: {db_e}")

async def main():
    pool = await asyncpg.create_pool(DB_URL)
    retry_delay = 1
    while True:
        try:
            await collect_liquidations(pool)
            retry_delay = 1
        except Exception as e:
            logger.error(f"Connection error: {e}. Retrying in {retry_delay}s...")
            await asyncio.sleep(retry_delay)
            retry_delay = min(60, retry_delay * 2)

if __name__ == "__main__":
    asyncio.run(main())
