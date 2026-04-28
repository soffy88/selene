"""Read sel_features from TimescaleDB for a given time range and symbol."""
import logging
from datetime import datetime
from typing import Optional

import asyncpg

logger = logging.getLogger(__name__)

_SELECT_FEATURES_SQL = """
SELECT
    time, symbol,
    close, delta_p_pct, sigma_p_24h,
    H, H_sample_count,
    top_5_bid_size, top_5_ask_size, total_depth, spread_bps,
    TF, OI, funding_rate,
    LV, absorption_ratio,
    price_autocorr_12h, price_autocorr_24h, price_autocorr_48h,
    sigma_p_d2, H_change_rate_std_12h, OI_hurst,
    data_quality, computed_at
FROM sel_features
WHERE symbol = $1
  AND time >= $2
  AND time <= $3
ORDER BY time ASC
"""


async def read_features(
    pool: asyncpg.Pool,
    symbol: str,
    start: datetime,
    end: datetime,
) -> list[dict]:
    """Return list of feature dicts for the given symbol and time range."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(_SELECT_FEATURES_SQL, symbol, start, end)
    return [dict(row) for row in rows]


async def read_closes(
    pool: asyncpg.Pool,
    symbol: str,
    end: datetime,
    limit: int = 100,
) -> list[float]:
    """Return closing prices from candles table, oldest first, up to `limit` bars ending at `end`."""
    sql = """
        SELECT close FROM candles
        WHERE symbol = $1 AND interval = '1h' AND time <= $2
        ORDER BY time DESC
        LIMIT $3
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, symbol, end, limit)
    return [float(r["close"]) for r in reversed(rows)]


async def read_sigma_p_history(
    pool: asyncpg.Pool,
    symbol: str,
    end: datetime,
    limit: int = 50,
) -> list[float]:
    """Return historical sigma_p_24h values from sel_features, oldest first."""
    sql = """
        SELECT sigma_p_24h FROM sel_features
        WHERE symbol = $1 AND time < $2 AND sigma_p_24h IS NOT NULL
        ORDER BY time DESC
        LIMIT $3
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, symbol, end, limit)
    return [float(r["sigma_p_24h"]) for r in reversed(rows)]


async def read_H_history(
    pool: asyncpg.Pool,
    symbol: str,
    end: datetime,
    limit: int = 50,
) -> list[float]:
    """Return historical H values from sel_features, oldest first."""
    sql = """
        SELECT H FROM sel_features
        WHERE symbol = $1 AND time < $2 AND H IS NOT NULL
        ORDER BY time DESC
        LIMIT $3
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, symbol, end, limit)
    return [float(r["h"]) for r in reversed(rows)]


async def read_OI_history(
    pool: asyncpg.Pool,
    symbol: str,
    end: datetime,
    limit: int = 50,
) -> list[float]:
    """Return historical OI values from sel_oi_history, oldest first."""
    sql = """
        SELECT oi_value FROM sel_oi_history
        WHERE symbol = $1 AND time <= $2
        ORDER BY time DESC
        LIMIT $3
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, symbol, end, limit)
    return [float(r["oi_value"]) for r in reversed(rows)]


async def read_depth_24h_means(
    pool: asyncpg.Pool,
    symbol: str,
    end: datetime,
) -> tuple[Optional[float], Optional[float]]:
    """Return (mean total_depth, mean spread_bps) over the past 24H bars."""
    sql = """
        SELECT AVG(total_depth) AS md, AVG(spread_bps) AS ms
        FROM sel_features
        WHERE symbol = $1
          AND time < $2
          AND time >= $2 - INTERVAL '24 hours'
          AND total_depth IS NOT NULL
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(sql, symbol, end)
    if row is None:
        return None, None
    md = float(row["md"]) if row["md"] is not None else None
    ms = float(row["ms"]) if row["ms"] is not None else None
    return md, ms
