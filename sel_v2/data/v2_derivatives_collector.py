import asyncio
import logging
import os
from datetime import datetime, timezone
import aiohttp
import asyncpg

from sel_v2.db.migrations import apply_schema
from sel_v2.data.insert_guard import InsertGuard, InsertFailureLimitExceeded

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("v2_derivatives_collector")

_guard = InsertGuard("v2_derivatives_snapshots")

DB_URL = os.environ.get("DB_URL")
BASE_SYMBOL = os.environ.get("SYMBOLS", "BTC-USDT")          # stored symbol
INST_ID = os.environ.get("DERIV_INST_ID", f"{BASE_SYMBOL}-SWAP")  # OKX SWAP instId
BASE_URL = "https://www.okx.com"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)


def _opt_float(d, key):
    """float(d[key]) or None when the field is absent/blank — never a 0.0 that
    reads as a real value."""
    if not d:
        return None
    v = d.get(key)
    if v in (None, ""):
        return None
    return float(v)


def build_deriv_row(funding_data, oi_data, mark_data, symbol, now_ms):
    """Map OKX REST responses to a v2_derivatives_snapshots row tuple, or None if
    the required funding+OI are missing. Missing mark/index price → None (NULL),
    not 0.0: a $0 price is a valid-looking value that silently corrupts any
    basis/premium (mark-index) calculation downstream."""
    if not (funding_data and oi_data):
        return None
    ts_val = max(int(oi_data.get("ts", 0)), int(funding_data.get("ts", 0))) or now_ms
    ts = datetime.fromtimestamp(ts_val / 1000, tz=timezone.utc)
    return (ts, symbol,
            _opt_float(funding_data, "fundingRate"),
            _opt_float(oi_data, "oi"),
            _opt_float(mark_data, "markPx"),
            _opt_float(mark_data, "idxPx"))


async def fetch_data(session, path):
    async with session.get(f"{BASE_URL}{path}?instId={INST_ID}",
                           timeout=REQUEST_TIMEOUT) as response:
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
                
                now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
                row = build_deriv_row(funding_data, oi_data, mark_data, BASE_SYMBOL, now_ms)
                if row is not None:
                    try:
                        await pool.execute(
                            "INSERT INTO v2_derivatives_snapshots (timestamp, symbol, funding_rate, open_interest, mark_price, index_price) VALUES ($1, $2, $3, $4, $5, $6) ON CONFLICT DO NOTHING",
                            *row
                        )
                        _guard.ok()
                        insert_count += 1
                        if insert_count == 1:
                            logger.info("INSERT row 1 to v2_derivatives_snapshots")
                        if insert_count % 10 == 0:
                            logger.info(f"v2_derivatives_snapshots cumulative inserts: {insert_count}")
                    except Exception as db_e:
                        _guard.fail(db_e)
            except InsertFailureLimitExceeded:
                raise  # fail-fast: let the process exit so the fault surfaces
            except Exception as e:
                logger.error(f"Error fetching derivatives: {e}")

            await asyncio.sleep(30)

if __name__ == "__main__":
    asyncio.run(main())
