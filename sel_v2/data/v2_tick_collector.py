import asyncio
import json
import logging
import os
from datetime import datetime, timezone

import asyncpg

from sel_v2.data.insert_guard import InsertFailureLimitExceeded, InsertGuard
from sel_v2.db.migrations import apply_schema

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("v2_tick_collector")

_guard = InsertGuard("v2_ticks")

DB_URL = os.environ.get("DB_URL")
# Stored symbol (everything downstream joins on this base symbol).
BASE_SYMBOL = os.environ.get("SYMBOLS", "BTC-USDT")
# OKX instId we actually subscribe to. The strategy trades the PERPETUAL, and
# OI/funding/liquidations are already collected from the swap — so the price &
# microstructure feed must be the swap too, not spot BTC-USDT, or the whole state
# machine runs on a different instrument than it trades (spot/perp basis drift).
INST_ID = os.environ.get("TICK_INST_ID", f"{BASE_SYMBOL}-SWAP")
PROXY_URL = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")


async def collect_ticks(pool):
    from websockets_proxy import Proxy, proxy_connect  # lazy: keep module import-safe without the proxy lib

    insert_count = 0
    proxy = Proxy.from_url(PROXY_URL) if PROXY_URL else None

    async with proxy_connect("wss://ws.okx.com:8443/ws/v5/public", proxy=proxy) as ws:
        await ws.send(json.dumps({"op": "subscribe", "args": [{"channel": "trades", "instId": INST_ID}]}))
        logger.info(f"Subscribed to trades for {INST_ID} (stored as {BASE_SYMBOL}) via proxy: {PROXY_URL}")

        async for msg in ws:
            data = json.loads(msg)
            if "data" in data:
                for t in data["data"]:
                    ts = datetime.fromtimestamp(int(t["ts"]) / 1000, tz=timezone.utc)
                    price = float(t["px"])
                    size = float(t["sz"])
                    side = t["side"]
                    trade_id = t["tradeId"]

                    try:
                        await pool.execute(
                            "INSERT INTO v2_ticks (timestamp, symbol, price, size, side, trade_id) VALUES ($1, $2, $3, $4, $5, $6) ON CONFLICT DO NOTHING",
                            ts,
                            BASE_SYMBOL,
                            price,
                            size,
                            side,
                            trade_id,
                        )
                        _guard.ok()
                        insert_count += 1
                        if insert_count == 1:
                            logger.info("INSERT row 1 to v2_ticks")
                        if insert_count % 100 == 0:
                            logger.info(f"v2_ticks cumulative inserts: {insert_count}")
                    except Exception as db_e:
                        _guard.fail(db_e)


async def main():
    pool = await asyncpg.create_pool(DB_URL)
    await apply_schema(pool)
    retry_delay = 1
    while True:
        try:
            await collect_ticks(pool)
            retry_delay = 1
        except InsertFailureLimitExceeded:
            raise  # fail-fast: let the process exit so the fault surfaces
        except Exception as e:
            logger.error(f"Connection error: {e}. Retrying in {retry_delay}s...")
            await asyncio.sleep(retry_delay)
            retry_delay = min(60, retry_delay * 2)


if __name__ == "__main__":
    asyncio.run(main())
