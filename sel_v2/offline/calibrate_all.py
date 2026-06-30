"""
Calibration backfill orchestrator (P1-7).

Runs every offline calibration that produces a live strategy parameter, on a real
historical bar series, and persists the results into v2_strategy_params — replacing
the textbook placeholders the strategies ship with (CUSUM/Hawkes/TDA thresholds
self-labelled "calibrate after paper Month 1"):

  H2 Hawkes  -> h2_mu_ref / h2_alpha_ref / h2_beta_ref / h2_branching_ratio_threshold
  TDA1 L^1   -> tda1_l1_threshold_p90 / p95 / p97

`load_strategy_params` (sel_v2/scheduler/replay.py, hawkes_intensity) reads these;
without them the live system falls back to the hardcoded defaults in
sel_v2/states/schema.py (hawkes_br_threshold=0.85, tda_l1_threshold=0.000097) and
Strategy 2 stays disabled.

Bars come from a parquet (`--data`) or are exported from v2_bars_4h (`--from-db`).

Usage:
    python -m sel_v2.offline.calibrate_all --data analysis/data/btc_4h.parquet
    python -m sel_v2.offline.calibrate_all --from-db        # pull bars from v2_bars_4h
"""
from __future__ import annotations

import argparse
import logging
import os
from typing import Optional

logger = logging.getLogger("calibrate_all")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# The full set of v2_strategy_params keys this backfill is responsible for. A deploy
# can check coverage against the live table after running (see verify_params_present).
EXPECTED_PARAM_KEYS = (
    "h2_mu_ref", "h2_alpha_ref", "h2_beta_ref", "h2_branching_ratio_threshold",
    "tda1_l1_threshold_p90", "tda1_l1_threshold_p95", "tda1_l1_threshold_p97",
)


def run_all(data_path: str, *, persist: bool = True, db_url: Optional[str] = None,
            hawkes_kwargs: Optional[dict] = None,
            tda_kwargs: Optional[dict] = None) -> dict:
    """Run the H2 Hawkes and TDA1 calibrations on `data_path` and (optionally)
    persist their parameters. Returns the computed values + the param keys written."""
    from sel_v2.offline.hawkes_calibration import run_hawkes_calibration
    from sel_v2.offline.tda_calibration import run_tda_calibration

    logger.info("Calibration backfill starting (data=%s persist=%s)", data_path, persist)
    h = run_hawkes_calibration(data_path=data_path, persist=persist, db_url=db_url,
                               **(hawkes_kwargs or {}))
    t = run_tda_calibration(data_path=data_path, persist=persist, db_url=db_url,
                            **(tda_kwargs or {}))
    full = h.get("full_fit", {}) or {}
    summary = {
        "hawkes": {"mu": full.get("mu"), "alpha": full.get("alpha"),
                   "beta": full.get("beta"), "branching_ratio": full.get("branching_ratio")},
        "tda": {"l1_q90": t.get("l1_q90"), "l1_q95": t.get("l1_q95"), "l1_q97": t.get("l1_q97")},
        "expected_param_keys": list(EXPECTED_PARAM_KEYS),
        "persisted": persist,
    }
    logger.info("Calibration backfill done: hawkes η=%.3f tda p95=%.6f",
                summary["hawkes"]["branching_ratio"] or float("nan"),
                summary["tda"]["l1_q95"] or float("nan"))
    return summary


async def export_bars_parquet(pool, parquet_path: str, symbol: str = "BTC-USDT") -> int:
    """Export v2_bars_4h for `symbol` to a parquet the calibrations can read.
    Returns the number of bars written."""
    import pandas as pd
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT time, open, high, low, close, volume FROM v2_bars_4h "
            "WHERE symbol=$1 ORDER BY time ASC", symbol)
    df = pd.DataFrame([dict(r) for r in rows])
    if not df.empty:
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = df[col].astype(float)
    os.makedirs(os.path.dirname(parquet_path) or ".", exist_ok=True)
    df.to_parquet(parquet_path)
    logger.info("Exported %d bars to %s", len(df), parquet_path)
    return len(df)


async def verify_params_present(pool) -> dict:
    """Deploy check: which EXPECTED_PARAM_KEYS exist in v2_strategy_params.
    Returns {present, missing, ok}."""
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT param_key FROM v2_strategy_params")
    have = {r["param_key"] for r in rows}
    present = [k for k in EXPECTED_PARAM_KEYS if k in have]
    missing = [k for k in EXPECTED_PARAM_KEYS if k not in have]
    return {"present": present, "missing": missing, "ok": not missing}


def main() -> None:
    parser = argparse.ArgumentParser(description="Run all calibrations and backfill params")
    parser.add_argument("--data", default="analysis/data/btc_4h.parquet",
                        help="Parquet bar file (ignored when --from-db is set)")
    parser.add_argument("--from-db", action="store_true",
                        help="Export bars from v2_bars_4h before calibrating")
    parser.add_argument("--symbol", default="BTC-USDT")
    parser.add_argument("--no-persist", action="store_true",
                        help="Compute but do not write to v2_strategy_params")
    parser.add_argument("--db-url", default=None, help="Postgres DSN override")
    args = parser.parse_args()

    data_path = args.data
    if args.from_db:
        import asyncio
        import asyncpg
        dsn = args.db_url or os.environ.get("DB_URL")
        data_path = "analysis/data/_calib_bars_from_db.parquet"

        async def _export():
            pool = await asyncpg.create_pool(dsn)
            try:
                return await export_bars_parquet(pool, data_path, args.symbol)
            finally:
                await pool.close()
        n = asyncio.run(_export())
        if n == 0:
            logger.error("v2_bars_4h returned 0 bars for %s — aborting", args.symbol)
            return

    run_all(data_path, persist=not args.no_persist, db_url=args.db_url)


if __name__ == "__main__":
    main()
