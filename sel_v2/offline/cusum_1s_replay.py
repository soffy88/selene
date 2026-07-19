"""CUSUM-Short replay at its DESIGNED granularity (Wave S2G-0).

The audit established that `cusum_short.py` documents itself as a Page CUSUM over
**1-second tick return z-scores** (its threshold window is literally 7*24*3600
"1-sec bars"), while the live engine feeds it **4H bar** z-scores. This module
replays the same accumulator over the 1s series the tick store actually holds, to
measure what the designed granularity would have produced.

MEASUREMENT, NOT A GATE. Low numbers do not condemn the design: the replay window
sits in a dead-water regime (σ percentile collapsed to ~0 by 07-18), so these
readings are a LOWER bound for a typical environment.

Reuses, never reimplements:
  * `CUSUMShort.update()` — the accumulator, with every module constant left as
    shipped (drift k, 95th-pct quantile, 604800s threshold window, <20-peak static
    2.0 guard).
  * `inverse_vocab.detect_absorption` / `detect_sweep` / `classify_entry_type`
    for the Step 3 simulation.

One choice the module does NOT make for us: it accepts z_t = (r_t - μ_t)/σ_t but
defines no standardisation window. Per the Wave, a 7-day rolling window is used
and flagged here. Note the live engine's `_zscore` is r/σ — i.e. μ≡0 — so this
replay's μ = rolling mean is a documented deviation from live, matching the
module's stated contract instead.

Offline and read-only: writes `v2_sim_cusum1s_triggers` (a simulation table) and
never touches v2_cusum_events, live tables, strategies/**, states/** or the epoch.

Run:  python -m sel_v2.offline.cusum_1s_replay
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import timezone

import asyncpg
import numpy as np
import pandas as pd

from sel_v2.strategies.cusum_short import CUSUMShort

logger = logging.getLogger(__name__)

SYMBOL = os.environ.get("SYMBOLS", "BTC-USDT")

# z-score standardisation window — NOT a module constant (see docstring).
ZSCORE_WINDOW_SEC = 7 * 24 * 3600
# Let z-scores start after an hour rather than discarding the first 7 days; with
# only ~13 days of ticks a strict 7-day burn-in would leave almost nothing.
ZSCORE_MIN_PERIODS = 3600

CREATE_TRIGGERS = """
CREATE TABLE IF NOT EXISTS v2_sim_cusum1s_triggers (
    replayed_at   timestamptz NOT NULL,
    trigger_ts    timestamptz NOT NULL,
    direction     text        NOT NULL,
    peak_value    numeric,
    threshold_h   numeric,
    warm          boolean     NOT NULL,
    PRIMARY KEY (replayed_at, trigger_ts, direction)
)
"""

CREATE_HT = """
CREATE TABLE IF NOT EXISTS v2_sim_cusum1s_ht (
    replayed_at timestamptz NOT NULL,
    minute      timestamptz NOT NULL,
    threshold_h numeric,
    n_peaks     integer,
    PRIMARY KEY (replayed_at, minute)
)
"""


def _dsn() -> str:
    return os.environ["DB_URL"].replace("postgresql+asyncpg://", "postgresql://")


async def load_1s_series(conn: asyncpg.Connection) -> pd.DataFrame:
    """Per-second last trade price on a gap-free grid, forward-filled."""
    rows = await conn.fetch(
        """
        SELECT date_trunc('second', timestamp) AS s,
               (array_agg(price ORDER BY timestamp DESC))[1] AS px
        FROM v2_ticks WHERE symbol = $1
        GROUP BY 1 ORDER BY 1
        """,
        SYMBOL,
    )
    if not rows:
        return pd.DataFrame(columns=["s", "px"])
    df = pd.DataFrame(
        {"s": [r["s"] for r in rows], "px": [float(r["px"]) for r in rows]}
    )
    grid = pd.date_range(df["s"].iloc[0], df["s"].iloc[-1], freq="1s", tz=timezone.utc)
    df = df.set_index("s").reindex(grid)
    df["px"] = df["px"].ffill()  # no trade this second → price did not move
    return df.dropna().rename_axis("s").reset_index()


def zscores(px: np.ndarray) -> np.ndarray:
    """1s log returns standardised on a 7-day rolling window (see docstring)."""
    r = np.diff(np.log(px), prepend=np.log(px[0]))
    s = pd.Series(r)
    mu = s.rolling(ZSCORE_WINDOW_SEC, min_periods=ZSCORE_MIN_PERIODS).mean()
    sd = s.rolling(ZSCORE_WINDOW_SEC, min_periods=ZSCORE_MIN_PERIODS).std()
    z = (s - mu) / sd
    return z.replace([np.inf, -np.inf], np.nan).fillna(0.0).to_numpy()


def replay(ts: pd.Series, z: np.ndarray) -> tuple[list, list, int | None]:
    """Feed the 1s z-series through the SHIPPED accumulator, unmodified.

    Returns (triggers, ht_track, warm_index) where warm_index is the step at which
    the 20th peak landed (the module's <20-peak static-2.0 guard lifting).
    """
    acc = CUSUMShort()
    triggers: list = []
    ht: list = []
    warm_at: int | None = None
    unix = ts.astype("int64").to_numpy() // 1_000_000_000

    for i in range(len(z)):
        t = float(unix[i])
        trig = acc.update(float(z[i]), t)
        n_peaks = len(acc._pos_peaks) + len(acc._neg_peaks)
        if warm_at is None and n_peaks >= 20:
            warm_at = i
        if trig.triggered:
            triggers.append(
                {
                    "ts": ts.iloc[i],
                    "direction": trig.direction,
                    "peak": max(trig.cusum_positive, trig.cusum_negative),
                    "h": trig.threshold,
                    "warm": warm_at is not None,
                }
            )
        if i % 60 == 0:  # minute-sampled h_t trajectory
            ht.append({"minute": ts.iloc[i], "h": trig.threshold, "n_peaks": n_peaks})
    return triggers, ht, warm_at


async def main() -> None:
    conn = await asyncpg.connect(_dsn())
    try:
        await conn.execute(CREATE_TRIGGERS)
        await conn.execute(CREATE_HT)
        df = await load_1s_series(conn)
        if df.empty:
            logger.error("no ticks — nothing to replay")
            return
        logger.info(
            "1s grid: %d seconds, %s .. %s",
            len(df),
            df["s"].iloc[0],
            df["s"].iloc[-1],
        )
        z = zscores(df["px"].to_numpy())
        logger.info(
            "z: finite=%d |z|>2 count=%d",
            int(np.isfinite(z).sum()),
            int((np.abs(z) > 2).sum()),
        )

        triggers, ht, warm_at = replay(df["s"], z)
        logger.info(
            "triggers=%d  warm_at_step=%s  final_h=%s",
            len(triggers),
            warm_at,
            ht[-1]["h"] if ht else None,
        )

        replayed_at = pd.Timestamp.utcnow().to_pydatetime()
        if triggers:
            await conn.executemany(
                "INSERT INTO v2_sim_cusum1s_triggers "
                "(replayed_at,trigger_ts,direction,peak_value,threshold_h,warm) "
                "VALUES ($1,$2,$3,$4::numeric,$5::numeric,$6) ON CONFLICT DO NOTHING",
                [
                    (
                        replayed_at,
                        t["ts"].to_pydatetime(),
                        t["direction"],
                        t["peak"],
                        t["h"],
                        t["warm"],
                    )
                    for t in triggers
                ],
            )
        await conn.executemany(
            "INSERT INTO v2_sim_cusum1s_ht (replayed_at,minute,threshold_h,n_peaks) "
            "VALUES ($1,$2,$3::numeric,$4) ON CONFLICT DO NOTHING",
            [
                (replayed_at, h["minute"].to_pydatetime(), h["h"], h["n_peaks"])
                for h in ht
            ],
        )
        logger.info("wrote %d triggers, %d h_t samples", len(triggers), len(ht))
    finally:
        await conn.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    asyncio.run(main())
