"""Binance USD-M REST helpers for the live collectors (2026-07-03 OKX→Binance migration).

Why REST, not WebSocket: OKX is unreachable in this deployment, and Binance's WS host
(`fstream.binance.com`) is NOT reachable through the only working proxy `helios-proxy:2080`
— the CONNECT tunnel establishes but the long-lived TLS/WS stream is reset (empirically:
5/5 handshakes fail with Timeout/ConnReset). Binance REST (`fapi.binance.com`) IS reachable.
A flaky proxy actually suits polling better than streaming: each poll retries independently,
there is no persistent connection to drop. See `audit/2026-07-03_full_health_audit.md`.

Proxy: we deliberately use a dedicated BINANCE_PROXY (default the known-good helios-proxy)
instead of the container's HTTPS_PROXY, because HTTPS_PROXY still carries the dead OKX proxy
from the protected .env. This lets a plain `docker restart` bring the migrated collector up
with a working egress, no .env edit required.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

BINANCE_FAPI = os.environ.get("BINANCE_FAPI_BASE", "https://fapi.binance.com")
BINANCE_PROXY = os.environ.get("BINANCE_PROXY", "http://helios-proxy:2080")


def to_binance_symbol(base_symbol: str) -> str:
    """'BTC-USDT' (stored base) -> 'BTCUSDT' (Binance perp symbol)."""
    return base_symbol.replace("-", "").upper()


def _opt_float(d: dict | None, key: str):
    """float(d[key]) or None when absent/blank — never a 0.0 that reads as real."""
    if not d:
        return None
    v = d.get(key)
    if v in (None, ""):
        return None
    return float(v)


async def fetch_json(
    session, path: str, params: dict | None = None, *, retries: int = 3
):
    """GET fapi{path} through BINANCE_PROXY, return parsed JSON (dict/list) or None.
    The proxy is flaky (intermittent TLS reset/timeout even on REST), so retry a few
    times with short backoff before giving up — a single dropped request must not cost
    a whole poll cycle. Returns None after `retries` failed attempts (caller skips)."""
    url = f"{BINANCE_FAPI}{path}"
    last_exc = None
    for attempt in range(retries):
        try:
            async with session.get(url, params=params, proxy=BINANCE_PROXY) as resp:
                if resp.status != 200:
                    return None
                return await resp.json()
        except Exception as e:  # transient proxy TLS reset / timeout
            last_exc = e
            await asyncio.sleep(0.5 * (attempt + 1))
    if last_exc is not None:
        raise last_exc
    return None


def build_deriv_row_binance(
    premium: dict | None, oi: dict | None, symbol: str, now_ms: int
):
    """Map Binance premiumIndex + openInterest to a v2_derivatives_snapshots row
    (timestamp, symbol, funding_rate, open_interest, mark_price, index_price), or None
    when the required funding/OI are missing. premiumIndex carries mark+index+funding in
    one call; openInterest carries OI. Missing price → None (NULL), not a $0 that would
    silently corrupt any basis (mark-index) downstream."""
    if not (premium and oi):
        return None
    ts_ms = int(premium.get("time") or oi.get("time") or now_ms)
    ts = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    return (
        ts,
        symbol,
        _opt_float(premium, "lastFundingRate"),
        _opt_float(oi, "openInterest"),
        _opt_float(premium, "markPrice"),
        _opt_float(premium, "indexPrice"),
    )
