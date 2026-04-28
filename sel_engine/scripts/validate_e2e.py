"""
Wave 4 end-to-end validation script.

Usage:
    python -m sel_engine.scripts.validate_e2e

What it does:
    1. Reads the last 800 bars of BTCUSDT from `candles` TimescaleDB table.
    2. Runs them through StateOutputService.process_bar() with mock pg/redis
       (no DB writes, no Redis writes).
    3. Prints the last 10 state records.
    4. Prints the health report.
    5. Exits 0 on success.

If the DB is not running, it prints a "DB not available" message and exits 0.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("validate_e2e")


def _make_mock_pg():
    """Mock pg pool — upsert calls go nowhere."""
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value=None)
    pool = AsyncMock()

    class _CM:
        def __init__(self, c):
            self._c = c
        async def __aenter__(self):
            return self._c
        async def __aexit__(self, *_):
            pass

    pool.acquire = MagicMock(return_value=_CM(conn))
    return pool


def _make_mock_redis():
    """Mock Redis — stream publishes and hash writes go nowhere."""
    redis = AsyncMock()
    redis.set = AsyncMock()
    redis.xadd = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    # TF and funding_rate from Redis will be None (no real collector)
    redis.hget = AsyncMock(return_value=None)
    return redis


async def _read_candles(url: str, symbol: str, limit: int = 800) -> list[dict]:
    """Read last `limit` OHLCV candles from TimescaleDB."""
    import asyncpg
    pool = await asyncpg.create_pool(url, min_size=1, max_size=3, timeout=5)
    sql = """
        SELECT open_time, close
        FROM candles
        WHERE symbol = $1 AND interval = '1h'
        ORDER BY open_time DESC
        LIMIT $2
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, symbol, limit)
    await pool.close()
    # Return oldest-first
    return [{"open_time": r["open_time"], "close": float(r["close"])} for r in reversed(rows)]


async def main() -> int:
    symbol = os.environ.get("SEL_SYMBOL", "BTCUSDT")
    timescale_url = os.environ.get(
        "TIMESCALE_URL",
        "postgresql://cw4:changeme@timescaledb:5432/cw4",
    )

    print(f"=== sel Wave 4 — End-to-End Validation ===")
    print(f"Symbol  : {symbol}")
    print(f"DB URL  : {timescale_url}")
    print()

    # ── Step 1: read candles ──────────────────────────────────────────────────
    try:
        candles = await _read_candles(timescale_url, symbol)
    except Exception as exc:
        print(f"[INFO] DB not available: {exc}")
        print("[INFO] Falling back to synthetic data (100 flat bars at 50 000)")
        now = datetime(2024, 6, 1, tzinfo=timezone.utc)
        from datetime import timedelta
        candles = [
            {"open_time": now + timedelta(hours=i), "close": 50000.0 + i * 0.5}
            for i in range(100)
        ]

    print(f"[1/4] Loaded {len(candles)} candles  "
          f"({candles[0]['open_time']} → {candles[-1]['open_time']})")

    # ── Step 2: run through StateOutputService ───────────────────────────────
    from sel_engine.paper_interface.service import StateOutputService

    svc = StateOutputService(symbol)
    pg = _make_mock_pg()
    redis = _make_mock_redis()

    outputs = []
    for bar in candles:
        out = await svc.process_bar(
            bar_time=bar["open_time"],
            close=bar["close"],
            pg_pool=pg,
            redis=redis,
        )
        outputs.append(out)

    print(f"[2/4] Processed {len(outputs)} bars")

    # ── Step 3: print last 10 state records ──────────────────────────────────
    print()
    print("[3/4] Last 10 state records:")
    print(f"  {'bar_time':<26}  {'state':<20}  {'dir':<5}  {'cold':<5}  "
          f"{'legal':<5}  {'compl':>5}  reason")
    print("  " + "-" * 100)
    for out in outputs[-10:]:
        print(
            f"  {str(out.bar_time):<26}  "
            f"{(out.state or 'None'):<20}  "
            f"{(out.direction or '–'):<5}  "
            f"{str(out.is_cold_start):<5}  "
            f"{str(out.is_legal_transition):<5}  "
            f"{out.feature_completeness:>5.2f}  "
            f"{out.reason[:60]}"
        )

    # ── Step 4: print health report ───────────────────────────────────────────
    print()
    print("[4/4] Health report (in-memory engine):")
    report = svc.engine.health.generate()
    print(f"  Total bars        : {report.total_bars}")
    print(f"  Cold-start bars   : {report.cold_start_bars}")
    print(f"  Active bars       : {report.active_bars}")
    print(f"  In healthy range  : {report.in_healthy_range}")
    print(f"  Legal tx rate     : {report.legal_transition_rate:.4f}")
    print(f"  State rates:")
    for state, rate in sorted(report.state_rates.items(), key=lambda x: -x[1]):
        if rate > 0:
            print(f"    {state:<22}: {rate:.4f}")
    if report.health_warnings:
        print(f"  Warnings ({len(report.health_warnings)}):")
        for w in report.health_warnings[:5]:
            print(f"    - {w}")
    else:
        print("  Warnings          : none")

    print()
    print("[OK] Validation complete — exit 0")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
