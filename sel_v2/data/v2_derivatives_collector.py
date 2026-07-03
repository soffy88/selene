import asyncio
import logging
import os
from datetime import datetime, timezone
import aiohttp
import asyncpg

from sel_v2.db.migrations import apply_schema
from sel_v2.data.insert_guard import InsertGuard, InsertFailureLimitExceeded
from sel_v2.data.binance_rest import (
    fetch_json,
    build_deriv_row_binance,
    to_binance_symbol,
    BINANCE_PROXY,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("v2_derivatives_collector")

_guard = InsertGuard("v2_derivatives_snapshots")

DB_URL = os.environ.get("DB_URL")
BASE_SYMBOL = os.environ.get("SYMBOLS", "BTC-USDT")  # stored symbol
# OKX is unreachable in this deployment (see binance_rest.py) — collect from Binance
# USD-M perp via REST polling. premiumIndex gives mark+index+funding in one call.
FETCH_SYMBOL = to_binance_symbol(BASE_SYMBOL)  # 'BTCUSDT'
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)


async def main():
    pool = await asyncpg.create_pool(DB_URL)
    await apply_schema(pool)
    insert_count = 0
    logger.info(
        f"derivatives collector: Binance REST {FETCH_SYMBOL} via {BINANCE_PROXY} "
        f"(stored as {BASE_SYMBOL})"
    )

    async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
        while True:
            try:
                premium = await fetch_json(
                    session, "/fapi/v1/premiumIndex", {"symbol": FETCH_SYMBOL}
                )
                oi_data = await fetch_json(
                    session, "/fapi/v1/openInterest", {"symbol": FETCH_SYMBOL}
                )

                now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
                row = build_deriv_row_binance(premium, oi_data, BASE_SYMBOL, now_ms)
                if row is not None:
                    try:
                        await pool.execute(
                            "INSERT INTO v2_derivatives_snapshots (timestamp, symbol, funding_rate, open_interest, mark_price, index_price) VALUES ($1, $2, $3, $4, $5, $6) ON CONFLICT DO NOTHING",
                            *row,
                        )
                        _guard.ok()
                        insert_count += 1
                        if insert_count == 1:
                            logger.info("INSERT row 1 to v2_derivatives_snapshots")
                        if insert_count % 10 == 0:
                            logger.info(
                                f"v2_derivatives_snapshots cumulative inserts: {insert_count}"
                            )
                    except Exception as db_e:
                        _guard.fail(db_e)
            except InsertFailureLimitExceeded:
                raise  # fail-fast: let the process exit so the fault surfaces
            except Exception as e:
                logger.error(f"Error fetching derivatives: {e}")

            await asyncio.sleep(30)


if __name__ == "__main__":
    asyncio.run(main())
