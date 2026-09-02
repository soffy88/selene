"""Binance USD-M perp 4H bar backfill for `v2_bars_4h`.

Why this exists (2026-07-03 full-health audit, item P0-a/P0-c):
  OKX is unreachable from this deployment — every proxy/host returns 403 / SSL-EOF
  / connection-reset, and the platform even ships `*_OKX_FLAG=0`. So `okx_backfill.py`
  (which hits www.okx.com and needs psycopg2, absent in the runtime image) cannot run
  here, and `v2_bars_4h` — the single most depended-on table (state machine, replay,
  offline Hawkes/TDA calibration, the SEL cockpit chart) — sat at 0 rows.

  Binance fapi IS reachable via `HTTPS_PROXY=http://helios-proxy:2080`, so we backfill
  the 4H bar foundation from Binance BTCUSDT perp. Measured OKX/Binance perp basis is
  ~0.05% (STATUS `708903d`), negligible for the σ / breakout / regime features that
  consume closes. Provenance is tracked explicitly via `source='binance'`.

Design (mirrors okx_backfill.py conventions):
  - stored `symbol` is the base ('BTC-USDT') so downstream joins are unchanged;
    fetched from the Binance perp symbol ('BTCUSDT').
  - `vwap` = NULL (history klines carry no VWAP — never fake a 0.0, audit P2-5).
  - `tick_count` = 0 (REST-sourced, not aggregated from live ticks — same convention
    the bar-aggregator uses to mark REST-recovered bars).
  - `ON CONFLICT (time, symbol) DO NOTHING` — idempotent, safe to re-run; never clobbers
    a live-aggregated bar that already exists for that timestamp.

Run (inside a container that has the proxy + asyncpg, e.g. the bar-aggregator):
  python -m sel_v2.data.binance_backfill --years 2
"""

from __future__ import annotations

import argparse
import asyncio
import os
import time
from datetime import datetime, timedelta, timezone

import asyncpg
import requests

DB_URL = os.environ.get("DB_URL", "postgresql://selene_app:sel_app_2026@platform-postgres:5432/selene")
BINANCE_FAPI = os.environ.get("BINANCE_FAPI_BASE", "https://fapi.binance.com")

# Binance interval token per stored bar size, and its width in ms (for forward paging).
_INTERVAL = {
    "4H": ("4h", 14_400_000),
    "1H": ("1h", 3_600_000),
    "1D": ("1d", 86_400_000),
}
_LIMIT = 1500  # Binance klines hard cap per request.


def _proxies() -> dict:
    # Prefer BINANCE_PROXY (known-good helios-proxy); fall back to HTTPS_PROXY only if it
    # isn't the dead OKX proxy. Keeps the backfill working on a plain run without a .env edit.
    px = os.environ.get("BINANCE_PROXY") or os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if px and "172.23.224.1" in px:  # the dead OKX proxy from .env
        px = "http://helios-proxy:2080"
    return {"https": px, "http": px} if px else {}


def _fetch(symbol: str, interval: str, start_ms: int, end_ms: int, *, retries: int = 4) -> list[list]:
    """One page of klines [openTime, o, h, l, c, v, closeTime, ...] ascending by time.
    Retries on the flaky proxy's intermittent TLS reset/timeout before giving up."""
    url = (
        f"{BINANCE_FAPI}/fapi/v1/klines?symbol={symbol}&interval={interval}"
        f"&startTime={start_ms}&endTime={end_ms}&limit={_LIMIT}"
    )
    last_exc = None
    for attempt in range(retries):
        try:
            resp = requests.get(url, proxies=_proxies(), timeout=20)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            last_exc = e
            time.sleep(0.6 * (attempt + 1))
    raise last_exc


async def backfill(stored_symbol: str, fetch_symbol: str, bar: str, years: int) -> int:
    interval, step_ms = _INTERVAL[bar]
    start_ms = int((datetime.now(timezone.utc) - timedelta(days=365 * years)).timestamp() * 1000)
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    conn = await asyncpg.connect(DB_URL)
    total = 0
    try:
        cursor = start_ms
        while cursor < end_ms:
            rows = _fetch(fetch_symbol, interval, cursor, end_ms)
            if not rows:
                break
            records = []
            for k in rows:
                ts = datetime.fromtimestamp(int(k[0]) / 1000, tz=timezone.utc)
                records.append(
                    (
                        ts,
                        stored_symbol,
                        float(k[1]),
                        float(k[2]),
                        float(k[3]),
                        float(k[4]),
                        float(k[5]),
                        None,
                        0,
                        "binance",
                    )
                )
            await conn.executemany(
                """INSERT INTO v2_bars_4h
                       (time, symbol, open, high, low, close, volume, vwap, tick_count, source)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                   ON CONFLICT (time, symbol) DO NOTHING""",
                records,
            )
            total += len(records)
            last_open = int(rows[-1][0])
            print(
                f"  +{len(records):4d} bars  total={total:5d}  through "
                f"{datetime.fromtimestamp(last_open / 1000, tz=timezone.utc):%Y-%m-%d %H:%M}"
            )
            if len(rows) < _LIMIT:
                break
            cursor = last_open + step_ms  # next bar after the last one returned
    finally:
        await conn.close()
    return total


def main() -> None:
    p = argparse.ArgumentParser(description="Backfill v2_bars_4h from Binance perp klines.")
    p.add_argument("--symbol", default="BTC-USDT", help="stored base symbol")
    p.add_argument(
        "--fetch-symbol",
        default=None,
        help="Binance perp symbol (default: symbol sans '-')",
    )
    p.add_argument("--bar", default="4H", choices=list(_INTERVAL))
    p.add_argument("--years", type=float, default=2)
    p.add_argument(
        "--loop",
        type=int,
        default=0,
        metavar="SECONDS",
        help="if >0, run forever: initial backfill then re-poll recent bars every "
        "SECONDS so v2_bars_4h stays fresh forward (the tick-fed aggregator is "
        "dead while OKX WS is unreachable). e.g. --loop 600 --years 0.02",
    )
    args = p.parse_args()
    fetch_symbol = args.fetch_symbol or args.symbol.replace("-", "")

    if args.loop > 0:
        print(
            f"Binance bar poller: {fetch_symbol} {args.bar} every {args.loop}s -> v2_bars_4h "
            f"(symbol={args.symbol}, source=binance)"
        )

        async def _run_forever():
            while True:
                try:
                    n = await backfill(args.symbol, fetch_symbol, args.bar, args.years)
                    print(f"  poll: {n} recent bars upserted", flush=True)
                except Exception as e:  # transient proxy failure — retry next cycle
                    print(f"  poll error (retry in {args.loop}s): {e}", flush=True)
                await asyncio.sleep(args.loop)

        asyncio.run(_run_forever())
        return

    print(
        f"Binance backfill: {fetch_symbol} {args.bar} x {args.years}y -> v2_bars_4h "
        f"(symbol={args.symbol}, source=binance)"
    )
    total = asyncio.run(backfill(args.symbol, fetch_symbol, args.bar, args.years))
    print(f"Done. Rows processed: {total} (existing rows kept via ON CONFLICT DO NOTHING).")


if __name__ == "__main__":
    main()
