import asyncio
import json
import logging
import math
import os
from datetime import datetime, timezone

import aiohttp
import asyncpg

from sel_v2.data.binance_rest import BINANCE_PROXY, fetch_json, to_binance_symbol
from sel_v2.data.insert_guard import InsertFailureLimitExceeded, InsertGuard
from sel_v2.db.migrations import apply_schema

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("v2_lob_collector")

_guard = InsertGuard("v2_lob_snapshots")

DB_URL = os.environ.get("DB_URL")
# Stored symbol (downstream joins on this base symbol).
BASE_SYMBOL = os.environ.get("SYMBOLS", "BTC-USDT")
# OKX WS is unreachable here (see binance_rest.py) — REST-poll the Binance USD-M perp
# book instead. Binance depth returns the same [price, qty] arrays OKX books5 did, so the
# stored shape + entropy/depth math are unchanged; we just sample once per LOB_POLL_S.
FETCH_SYMBOL = to_binance_symbol(BASE_SYMBOL)
LOB_POLL_S = float(os.environ.get("LOB_POLL_S", "60"))
LOB_LIMIT = int(os.environ.get("LOB_LIMIT", "20"))


def calc_entropy(bids, asks):
    sizes = [float(b[1]) for b in bids[:5]] + [float(a[1]) for a in asks[:5]]
    total = sum(sizes)
    if total == 0:
        return 0.0
    probs = [s / total for s in sizes if s > 0]
    return -sum(p * math.log2(p) for p in probs)


async def collect_lob(pool):
    insert_count = 0
    logger.info(
        f"LOB collector: Binance REST depth {FETCH_SYMBOL} limit={LOB_LIMIT} "
        f"every {LOB_POLL_S}s via {BINANCE_PROXY} (stored as {BASE_SYMBOL})"
    )
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                book = await fetch_json(
                    session,
                    "/fapi/v1/depth",
                    {"symbol": FETCH_SYMBOL, "limit": LOB_LIMIT},
                )
            except Exception as e:
                # transient proxy failure (after retries) — skip this poll, keep the loop
                logger.warning(f"depth fetch failed, skipping poll: {e}")
                await asyncio.sleep(LOB_POLL_S)
                continue
            if book and book.get("bids") and book.get("asks"):
                # Binance depth 'T' = book transaction time (ms); fall back to now.
                ts_ms = int(book.get("T") or datetime.now(timezone.utc).timestamp() * 1000)
                ts = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
                bids = book["bids"]
                asks = book["asks"]
                bid_depth = sum(float(b[1]) for b in bids[:5])
                ask_depth = sum(float(a[1]) for a in asks[:5])
                entropy = calc_entropy(bids, asks)
                try:
                    await pool.execute(
                        "INSERT INTO v2_lob_snapshots (timestamp, symbol, bids, asks, bid_depth, ask_depth, entropy) VALUES ($1, $2, $3, $4, $5, $6, $7) ON CONFLICT DO NOTHING",
                        ts,
                        BASE_SYMBOL,
                        json.dumps(bids),
                        json.dumps(asks),
                        bid_depth,
                        ask_depth,
                        entropy,
                    )
                    _guard.ok()
                    insert_count += 1
                    if insert_count == 1:
                        logger.info("INSERT row 1 to v2_lob_snapshots")
                    if insert_count % 100 == 0:
                        logger.info(f"v2_lob_snapshots cumulative inserts: {insert_count}")
                except Exception as db_e:
                    _guard.fail(db_e)
            await asyncio.sleep(LOB_POLL_S)


async def main():
    pool = await asyncpg.create_pool(DB_URL)
    await apply_schema(pool)
    retry_delay = 1
    while True:
        try:
            await collect_lob(pool)
            retry_delay = 1
        except InsertFailureLimitExceeded:
            raise  # fail-fast: let the process exit so the fault surfaces
        except Exception as e:
            logger.error(f"Connection error: {e}. Retrying in {retry_delay}s...")
            await asyncio.sleep(retry_delay)
            retry_delay = min(60, retry_delay * 2)


if __name__ == "__main__":
    asyncio.run(main())
