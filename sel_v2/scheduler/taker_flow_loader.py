"""
Helixa taker flow loader — 1m → 4H OFI proxy (sel v2 Wave 5).

Aggregates taker_buy_volume - taker_sell_volume from helixa.public.taker_flow_1m
into 4H net taker flow aligned to bar_timestamps.

Each 4H bar [bar_ts - 4H, bar_ts) is summed from 1m rows whose window_start
falls in that half-open interval.

Coverage: helixa taker_flow_1m starts 2026-04-27; earlier bars yield NaN.
"""
from __future__ import annotations

import logging
import os
from datetime import timezone, timedelta
from typing import Optional, Sequence

import numpy as np

logger = logging.getLogger(__name__)

_4H_SECONDS = 4 * 3600


def _helixa_db_url(override: Optional[str] = None) -> str:
    if override:
        return override
    if url := os.getenv("HELIXA_DB_URL"):
        return url
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("POSTGRES_PORT", "5434")
    user = os.getenv("POSTGRES_USER", "selene_app")
    password = os.getenv("POSTGRES_PASSWORD", "")
    return f"postgresql://{user}:{password}@{host}:{port}/helixa"


def _to_ns_array(timestamps: Sequence) -> np.ndarray:
    import pandas as pd

    result = []
    for ts in timestamps:
        if isinstance(ts, np.datetime64):
            result.append(pd.Timestamp(ts).value)
        elif hasattr(ts, "timestamp"):
            result.append(int(ts.timestamp() * 1e9))
        else:
            result.append(pd.Timestamp(ts).value)
    return np.array(result, dtype=np.int64)


async def load_taker_flow_series(
    bar_timestamps: Sequence,
    symbol: str = "BTC/USDT-SWAP",
    db_url: Optional[str] = None,
) -> dict[str, np.ndarray]:
    """
    Aggregate 1m taker flow into 4H net taker flow aligned to bar_timestamps.

    Parameters
    ----------
    bar_timestamps : sequence of bar close timestamps (end of bar)
    symbol         : helixa symbol string, e.g. "BTC/USDT-SWAP"
    db_url         : override for helixa DB URL

    Returns
    -------
    dict with key:
      'net_taker_flow' (n,) float64: buy_vol - sell_vol; NaN where no data
    """
    import asyncpg

    n = len(bar_timestamps)
    flow_out = np.full(n, np.nan)

    url = _helixa_db_url(db_url)
    try:
        conn = await asyncpg.connect(url)
        rows = await conn.fetch(
            """
            SELECT window_start, taker_buy_volume, taker_sell_volume
            FROM public.taker_flow_1m
            WHERE symbol = $1
            ORDER BY window_start
            """,
            symbol,
        )
        await conn.close()
    except Exception as exc:
        logger.warning(
            "taker_flow_loader: helixa connection failed (%s) — all NaN", exc
        )
        return {"net_taker_flow": flow_out}

    if not rows:
        logger.info("taker_flow_loader: 0 rows for symbol=%s", symbol)
        return {"net_taker_flow": flow_out}

    # Build 1m-row arrays in nanosecond ints
    row_ns = np.array(
        [
            int(
                r["window_start"]
                .replace(tzinfo=timezone.utc)
                .timestamp()
                * 1e9
            )
            if r["window_start"].tzinfo is None
            else int(r["window_start"].timestamp() * 1e9)
            for r in rows
        ],
        dtype=np.int64,
    )
    row_buy = np.array(
        [float(r["taker_buy_volume"]) if r["taker_buy_volume"] is not None else 0.0 for r in rows]
    )
    row_sell = np.array(
        [float(r["taker_sell_volume"]) if r["taker_sell_volume"] is not None else 0.0 for r in rows]
    )
    row_net = row_buy - row_sell

    _4h_ns = int(_4H_SECONDS * 1e9)
    bar_ns = _to_ns_array(bar_timestamps)

    for i, bns in enumerate(bar_ns):
        bar_start_ns = bns - _4h_ns
        # window_start in [bar_start_ns, bns)
        lo = int(np.searchsorted(row_ns, bar_start_ns, side="left"))
        hi = int(np.searchsorted(row_ns, bns, side="left"))
        if hi > lo:
            flow_out[i] = float(np.sum(row_net[lo:hi]))

    covered = int(np.sum(~np.isnan(flow_out)))
    logger.info(
        "taker_flow_loader: %d/%d bars have net taker flow (symbol=%s, 1m_rows=%d)",
        covered,
        n,
        symbol,
        len(rows),
    )
    return {"net_taker_flow": flow_out}
