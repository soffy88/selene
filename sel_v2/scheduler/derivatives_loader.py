"""
Helixa derivatives loader — OI + funding_rate asof join (sel v2 Wave 5).

Loads open_interest and funding_rate from helixa.public.derivatives_snapshots,
aligned to sel v2 4H bar timestamps via asof join (last snapshot_ts <= bar_ts).

Coverage: helixa data starts 2026-04-27; bars before that date yield NaN.
State machine treats None as STUB — conservative skip, no regression risk.
"""

from __future__ import annotations

import logging
import os
from datetime import timezone
from typing import Optional, Sequence

import numpy as np

logger = logging.getLogger(__name__)


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
    """Convert mixed timestamp sequence to numpy datetime64[ns] UTC array."""
    import pandas as pd

    result = []
    for ts in timestamps:
        if isinstance(ts, np.datetime64):
            result.append(pd.Timestamp(ts).value)  # nanoseconds
        elif hasattr(ts, "timestamp"):
            # datetime object
            result.append(int(ts.timestamp() * 1e9))
        else:
            result.append(pd.Timestamp(ts).value)
    return np.array(result, dtype=np.int64)


async def load_oi_funding_series(
    bar_timestamps: Sequence,
    symbol: str = "BTC",
    db_url: Optional[str] = None,
) -> dict[str, np.ndarray]:
    """
    Asof-join helixa derivatives_snapshots to bar_timestamps.

    Parameters
    ----------
    bar_timestamps : sequence of timestamps (datetime / np.datetime64 / pd.Timestamp)
    symbol         : helixa symbol string, e.g. "BTC"
    db_url         : override for helixa DB URL

    Returns
    -------
    dict with keys:
      'open_interest'  (n,) float64, NaN where no snapshot at or before bar_ts
      'funding_rate'   (n,) float64, NaN where no snapshot at or before bar_ts
    """
    import asyncpg

    n = len(bar_timestamps)
    oi_out = np.full(n, np.nan)
    fr_out = np.full(n, np.nan)

    url = _helixa_db_url(db_url)
    try:
        conn = await asyncpg.connect(url)
        rows = await conn.fetch(
            """
            SELECT snapshot_ts, open_interest, funding_rate
            FROM public.derivatives_snapshots
            WHERE symbol = $1
            ORDER BY snapshot_ts
            """,
            symbol,
        )
        await conn.close()
    except Exception as exc:
        logger.warning("derivatives_loader: helixa connection failed (%s) — all NaN", exc)
        return {"open_interest": oi_out, "funding_rate": fr_out}

    if not rows:
        logger.info("derivatives_loader: 0 rows for symbol=%s", symbol)
        return {"open_interest": oi_out, "funding_rate": fr_out}

    # Build sorted snapshot arrays in nanosecond int for fast searchsorted
    snap_ns = np.array(
        [
            int(r["snapshot_ts"].replace(tzinfo=timezone.utc).timestamp() * 1e9)
            if r["snapshot_ts"].tzinfo is None
            else int(r["snapshot_ts"].timestamp() * 1e9)
            for r in rows
        ],
        dtype=np.int64,
    )
    snap_oi = np.array([float(r["open_interest"]) if r["open_interest"] is not None else np.nan for r in rows])
    snap_fr = np.array([float(r["funding_rate"]) if r["funding_rate"] is not None else np.nan for r in rows])

    bar_ns = _to_ns_array(bar_timestamps)

    for i, bns in enumerate(bar_ns):
        idx = int(np.searchsorted(snap_ns, bns, side="right")) - 1
        if idx >= 0:
            oi_out[i] = snap_oi[idx]
            fr_out[i] = snap_fr[idx]

    covered = int(np.sum(~np.isnan(oi_out)))
    logger.info(
        "derivatives_loader: %d/%d bars have OI/funding coverage (symbol=%s, helixa_rows=%d)",
        covered,
        n,
        symbol,
        len(rows),
    )
    return {"open_interest": oi_out, "funding_rate": fr_out}
