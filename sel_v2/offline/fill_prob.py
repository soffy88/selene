"""First-passage fill-probability table (Wave EXEC-S, Part 1).

For the S2 limit-order shadow layer we need P(fill): given a limit resting δ×ATR_1h
away from the market, how often does price actually reach it within T minutes? This
module estimates that empirically from `v2_ticks` and writes `v2_fill_prob`.

Method — first passage, measured the same conservative way the shadow layer books a
fill (`sel_v2/shadow/shadow_exec.py`): a level counts as reached only when a trade
prints STRICTLY through it (buy: trade < L; sell: trade > L). Touching L is not a
fill — there is no queue model here, so equality is deliberately not enough.

  anchors   every ANCHOR_STEP_MIN minutes over the available tick history
  price m   the last trade of the anchor minute
  level L   m - δ·ATR (buy side, resting below) / m + δ·ATR (sell side, above)
  window    the T minutes AFTER the anchor minute, i.e. (k, k+T]
  filled    min(window) < L (buy) / max(window) > L (sell)

Minute-level extremes are exact for this test: whether price crossed L inside a
window depends only on the window's min/max, so aggregating ticks to per-minute
OHLC loses nothing and turns 13M+ ticks into ~19k rows.

ATR_1h is derived from those same minute bars (resampled to 1h, `compute_atr`
reused from `sel_v2.offline.substate`) rather than the V4 `candles` table — same
source as the crossing test, so δ and the passage test can never disagree, and
sel_v2 keeps no cross-stack dependency.

Read-only offline analysis. Writes `v2_fill_prob` and nothing else — never
strategies/**, states/**, any live table, or the epoch.

Run once now:  python -m sel_v2.offline.fill_prob
Run forever (weekly, Monday 01:30 UTC — after v2-capture-monitor's 00:30 slot so
the two never contend for the DB):
               python -m sel_v2.offline.fill_prob --serve
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import asyncpg
import numpy as np
import pandas as pd

from sel_v2.offline.substate import compute_atr

logger = logging.getLogger(__name__)

SYMBOL = os.environ.get("SYMBOLS", "BTC-USDT")

# ── frozen grid (Wave EXEC-S; do not "optimise") ──────────────────────────────
DELTA_GRID = [round(0.1 * i, 1) for i in range(1, 16)]  # 0.1 .. 1.5 × ATR_1h
HORIZONS_MIN = [15, 30, 60, 120]
SIDES = ("buy", "sell")
ANCHOR_STEP_MIN = 5

ATR_HOURS = 14  # ATR_1h window, matching compute_atr's default convention

# Weekly slot: one hour after v2-capture-monitor (Monday 00:30 UTC) so the two
# offline jobs run serially rather than competing for the same DB.
RUN_WEEKDAY = 0  # Monday
RUN_HOUR = 1
RUN_MINUTE = 30

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS v2_fill_prob (
    estimated_at  timestamptz NOT NULL,
    -- numeric(3,2), not bare numeric: a Python float lands as
    -- 0.29999999999999998889... and `WHERE delta_atr = 0.3` then matches nothing,
    -- which breaks the only way this table is meant to be read (lookup by δ).
    delta_atr     numeric(3,2) NOT NULL,
    horizon_min   integer      NOT NULL,
    side          text         NOT NULL,
    p_fill        numeric,
    n_samples     integer      NOT NULL,
    PRIMARY KEY (estimated_at, delta_atr, horizon_min, side)
)
"""


def _dsn() -> str:
    return os.environ["DB_URL"].replace("postgresql+asyncpg://", "postgresql://")


async def _load_minute_bars(conn: asyncpg.Connection) -> pd.DataFrame:
    """Per-minute OHLC from v2_ticks. `close` anchors the quote; `low`/`high` carry
    the extremes the first-passage test needs."""
    rows = await conn.fetch(
        """
        SELECT date_trunc('minute', timestamp) AS minute,
               min(price) AS low,
               max(price) AS high,
               (array_agg(price ORDER BY timestamp DESC))[1] AS close
        FROM v2_ticks
        WHERE symbol = $1
        GROUP BY 1
        ORDER BY 1
        """,
        SYMBOL,
    )
    if not rows:
        return pd.DataFrame(columns=["minute", "low", "high", "close"])
    df = pd.DataFrame(
        {
            "minute": [r["minute"] for r in rows],
            "low": [float(r["low"]) for r in rows],
            "high": [float(r["high"]) for r in rows],
            "close": [float(r["close"]) for r in rows],
        }
    )
    # Reindex onto a gap-free minute grid so "the next T minutes" is a fixed row
    # count. Quiet minutes carry no trades: forward-fill the close (price did not
    # move) and leave high/low equal to it, which cannot manufacture a crossing.
    full = pd.date_range(
        df["minute"].iloc[0], df["minute"].iloc[-1], freq="1min", tz=timezone.utc
    )
    df = df.set_index("minute").reindex(full)
    df["close"] = df["close"].ffill()
    df["high"] = df["high"].fillna(df["close"])
    df["low"] = df["low"].fillna(df["close"])
    return df.dropna(subset=["close"]).rename_axis("minute").reset_index()


def _atr_1h_per_minute(df: pd.DataFrame) -> np.ndarray:
    """ATR_1h evaluated on hourly bars, held constant across each hour's minutes.

    Shifted by one hour so an anchor only ever sees ATR from bars that CLOSED
    before it — otherwise the level would be sized using the very move it is about
    to be tested against.
    """
    hourly = (
        df.set_index("minute")
        .resample("1h")
        .agg({"high": "max", "low": "min", "close": "last"})
        .dropna()
    )
    if hourly.empty:
        return np.full(len(df), np.nan)
    atr = compute_atr(
        hourly["high"].to_numpy(),
        hourly["low"].to_numpy(),
        hourly["close"].to_numpy(),
        window=ATR_HOURS,
    )
    series = pd.Series(atr, index=hourly.index).shift(1)  # only closed bars
    return series.reindex(df["minute"], method="ffill").to_numpy()


def _window_extremes(arr: np.ndarray, horizon: int, kind: str) -> np.ndarray:
    """Extreme of arr over the `horizon` entries AFTER each index (exclusive of it).

    Returns NaN where the full window would run past the end of the data, so a
    truncated window can never be scored as "did not fill".
    """
    n = len(arr)
    out = np.full(n, np.nan)
    if n <= horizon:
        return out
    s = pd.Series(arr)
    # rolling() looks backwards; reverse to get a forward-looking window, then flip
    # back. shift(-1) drops the anchor minute itself from its own window.
    rev = s[::-1]
    rolled = (
        rev.rolling(horizon).min() if kind == "min" else rev.rolling(horizon).max()
    )[::-1]
    out = rolled.shift(-1).to_numpy()
    return out


async def run_once() -> int:
    """Recompute the whole grid and append one snapshot. Returns rows written."""
    dsn = _dsn()
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(CREATE_SQL)
        df = await _load_minute_bars(conn)
        if df.empty:
            logger.warning(
                "v2_ticks yielded no minute bars for %s — nothing to do", SYMBOL
            )
            return 0

        atr = _atr_1h_per_minute(df)
        close = df["close"].to_numpy()
        low = df["low"].to_numpy()
        high = df["high"].to_numpy()

        anchor_mask = np.zeros(len(df), dtype=bool)
        anchor_mask[::ANCHOR_STEP_MIN] = True
        anchor_mask &= np.isfinite(atr) & (atr > 0)

        estimated_at = datetime.now(timezone.utc)
        payload: list[tuple] = []
        for horizon in HORIZONS_MIN:
            win_low = _window_extremes(low, horizon, "min")
            win_high = _window_extremes(high, horizon, "max")
            complete = anchor_mask & np.isfinite(win_low) & np.isfinite(win_high)
            n = int(complete.sum())
            for delta in DELTA_GRID:
                offset = delta * atr
                # buy rests below and fills only on a strictly lower print
                buy_hit = win_low[complete] < (close - offset)[complete]
                sell_hit = win_high[complete] > (close + offset)[complete]
                for side, hit in (("buy", buy_hit), ("sell", sell_hit)):
                    p = float(hit.mean()) if n else None
                    # Decimal(str(delta)): keeps the grid value exact in numeric
                    payload.append(
                        (estimated_at, Decimal(str(delta)), horizon, side, p, n)
                    )

        await conn.executemany(
            "INSERT INTO v2_fill_prob "
            "(estimated_at, delta_atr, horizon_min, side, p_fill, n_samples) "
            "VALUES ($1,$2,$3,$4,$5::numeric,$6) "
            "ON CONFLICT (estimated_at, delta_atr, horizon_min, side) DO NOTHING",
            payload,
        )
        span = f"{df['minute'].iloc[0]:%Y-%m-%d} .. {df['minute'].iloc[-1]:%Y-%m-%d}"
        logger.info(
            "fill_prob: %d rows from %d minute bars (%s), anchors/horizon=%s",
            len(payload),
            len(df),
            span,
            {
                h: int(
                    (anchor_mask & np.isfinite(_window_extremes(low, h, "min"))).sum()
                )
                for h in HORIZONS_MIN
            },
        )
        return len(payload)
    finally:
        await conn.close()


def _seconds_until_next_run(now: datetime) -> float:
    days_ahead = (RUN_WEEKDAY - now.weekday()) % 7
    target = (now + timedelta(days=days_ahead)).replace(
        hour=RUN_HOUR, minute=RUN_MINUTE, second=0, microsecond=0
    )
    if target <= now:
        target += timedelta(days=7)
    return (target - now).total_seconds()


async def serve_forever() -> None:
    """Weekly scheduler. Mirrors capture_monitor.serve_forever: a bad week logs an
    ERROR, it never kills the loop."""
    logger.info(
        "fill_prob scheduler started (weekly, Monday %02d:%02d UTC)",
        RUN_HOUR,
        RUN_MINUTE,
    )
    while True:
        now = datetime.now(timezone.utc)
        delay = _seconds_until_next_run(now)
        logger.info(
            "next run in %.1f hours (at %s)",
            delay / 3600,
            now + timedelta(seconds=delay),
        )
        await asyncio.sleep(delay)
        try:
            await run_once()
        except Exception as exc:  # noqa: BLE001 — never let a bad week kill the scheduler
            logger.error("weekly fill-prob estimation failed: %s", exc)
        await asyncio.sleep(60)


if __name__ == "__main__":
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    if "--serve" in sys.argv:
        asyncio.run(serve_forever())
    else:
        asyncio.run(run_once())
