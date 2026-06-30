import asyncio
import json
import logging
import os
from datetime import datetime, timezone
import asyncpg

from sel_v2.db.migrations import apply_schema
from sel_v2.data.insert_guard import InsertGuard, InsertFailureLimitExceeded

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("v2_liquidation_collector")

_guard = InsertGuard("v2_liquidations")

DB_URL = os.environ.get("DB_URL")
# Stored symbol (base) vs the OKX swap instId we filter the SWAP feed by — already
# perp, kept consistent/configurable with the other v2 collectors.
BASE_SYMBOL = os.environ.get("SYMBOLS", "BTC-USDT")
INST_ID = os.environ.get("LIQ_INST_ID", f"{BASE_SYMBOL}-SWAP")
PROXY_URL = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")


def extract_liquidation_rows(data, inst_id, base_symbol):
    """Map an OKX liquidation-orders WS message to v2_liquidations row tuples.

    OKX puts ``instId`` on the OUTER item; ``details[].instId`` is always None — so filtering
    on ``detail.instId`` (the original bug) skipped every liquidation and left v2_liquidations
    empty, killing the Cascade liquidation-pulse defense. We filter on ``item.instId``.
    Pure (no I/O) so it's unit-testable. Returns (ts, symbol, side, size, price, loss) tuples.
    """
    rows = []
    for item in data.get("data", []):
        if item.get("instId") != inst_id:
            continue
        for d in item.get("details", []):
            ts = datetime.fromtimestamp(int(d["ts"]) / 1000, tz=timezone.utc)
            rows.append((
                ts, base_symbol, d["side"], float(d["sz"]),
                float(d["bkPx"]) if d.get("bkPx") else 0.0,
                float(d["bkLoss"]) if d.get("bkLoss") else 0.0,
            ))
    return rows


async def collect_liquidations(pool):
    from websockets_proxy import proxy_connect, Proxy   # lazy: keep module import-safe without the proxy lib
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
                for row in extract_liquidation_rows(data, INST_ID, BASE_SYMBOL):
                    try:
                        await pool.execute(
                            "INSERT INTO v2_liquidations (timestamp, symbol, side, size, price, loss) VALUES ($1, $2, $3, $4, $5, $6) ON CONFLICT DO NOTHING",
                            *row
                        )
                        _guard.ok()
                        insert_count += 1
                        if insert_count == 1:
                            logger.info("INSERT row 1 to v2_liquidations")
                        if insert_count % 10 == 0:
                            logger.info(f"v2_liquidations cumulative inserts: {insert_count}")
                    except Exception as db_e:
                        _guard.fail(db_e)

async def main():
    pool = await asyncpg.create_pool(DB_URL)
    await apply_schema(pool)
    retry_delay = 1
    while True:
        try:
            await collect_liquidations(pool)
            retry_delay = 1
        except InsertFailureLimitExceeded:
            raise  # fail-fast: let the process exit so the fault surfaces
        except Exception as e:
            logger.error(f"Connection error: {e}. Retrying in {retry_delay}s...")
            await asyncio.sleep(retry_delay)
            retry_delay = min(60, retry_delay * 2)

if __name__ == "__main__":
    asyncio.run(main())
