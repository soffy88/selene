"""
OFI / microstructure feature persister (P1-5).

The Strategy-2 OFI proxy and LOB imbalance were recomputed in-memory from v2_ticks
/ v2_lob_snapshots on every engine run — there was no stored, point-in-time feature
to read, audit, or re-derive. This collector aggregates the same per-4H-bar features
in SQL and upserts them into v2_ofi_features so they become a first-class persisted
feature (reproducible, point-in-time correct).

Aggregation mirrors paper_engine._load_microstructure_series:
  taker_net = Σ buy size − Σ sell size   (signed OFI proxy)
  taker_vol = Σ size                      (total taken volume)
  lob_imb   = mean(bid_depth − ask_depth) (order-book imbalance)
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import asyncpg

from sel_v2.db.migrations import apply_schema

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("ofi_persister")

DB_URL = os.environ.get("DB_URL")
SYMBOL = os.environ.get("SYMBOLS", "BTC-USDT")
LOOKBACK_DAYS = int(os.environ.get("OFI_LOOKBACK_DAYS", "120"))

_FLOW_SQL = (
    "SELECT time_bucket('4 hours', timestamp) AS b, "
    "  COALESCE(SUM(size) FILTER (WHERE side='buy'),0) "
    "  - COALESCE(SUM(size) FILTER (WHERE side='sell'),0) AS net, "
    "  COALESCE(SUM(size),0) AS vol "
    "FROM v2_ticks WHERE symbol=$1 AND timestamp >= $2 GROUP BY b"
)
_LOB_SQL = (
    "SELECT time_bucket('4 hours', timestamp) AS b, "
    "  AVG(bid_depth - ask_depth) AS imb "
    "FROM v2_lob_snapshots WHERE symbol=$1 AND timestamp >= $2 GROUP BY b"
)
_UPSERT_SQL = (
    "INSERT INTO v2_ofi_features (time, symbol, taker_net, taker_vol, lob_imb) "
    "VALUES ($1, $2, $3, $4, $5) "
    "ON CONFLICT (time, symbol) DO UPDATE SET "
    "  taker_net = EXCLUDED.taker_net, taker_vol = EXCLUDED.taker_vol, "
    "  lob_imb = EXCLUDED.lob_imb, updated_at = NOW()"
)


def _f(v) -> Optional[float]:
    return None if v is None else float(v)


def merge_ofi_rows(flow_rows, lob_rows) -> list[dict]:
    """Combine per-bucket taker-flow and LOB-imbalance rows into one feature row
    per bar, sorted ascending by bucket time. A bucket present in only one source
    leaves the other features None (unknown), never zero-filled."""
    out: dict = {}
    for r in flow_rows:
        out[r["b"]] = {"time": r["b"], "taker_net": _f(r["net"]), "taker_vol": _f(r["vol"]), "lob_imb": None}
    for r in lob_rows:
        d = out.setdefault(r["b"], {"time": r["b"], "taker_net": None, "taker_vol": None, "lob_imb": None})
        d["lob_imb"] = _f(r["imb"])
    return [out[k] for k in sorted(out)]


async def persist_ofi(pool, symbol: str = SYMBOL, since: Optional[datetime] = None) -> int:
    """Aggregate and upsert OFI features since `since` (default: LOOKBACK_DAYS ago).
    Returns the number of bar-feature rows written."""
    if since is None:
        since = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    async with pool.acquire() as conn:
        flow = await conn.fetch(_FLOW_SQL, symbol, since)
        lob = await conn.fetch(_LOB_SQL, symbol, since)
        rows = merge_ofi_rows(flow, lob)
        if rows:
            await conn.executemany(
                _UPSERT_SQL,
                [(r["time"], symbol, r["taker_net"], r["taker_vol"], r["lob_imb"]) for r in rows],
            )
    logger.info("persisted %d OFI feature rows for %s since %s", len(rows), symbol, since)
    return len(rows)


async def main() -> None:
    pool = await asyncpg.create_pool(DB_URL)
    await apply_schema(pool)
    interval = int(os.environ.get("OFI_PERSIST_INTERVAL_SEC", "3600"))
    while True:
        try:
            await persist_ofi(pool, SYMBOL)
        except Exception as exc:  # noqa: BLE001
            logger.error("OFI persist cycle failed: %s", exc)
        await asyncio.sleep(interval)


if __name__ == "__main__":
    asyncio.run(main())
