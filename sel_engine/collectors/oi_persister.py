"""
OI persister: reads open interest from OKX every 5 min and persists to sel_oi_history.

OKX endpoint: GET /api/v5/public/open-interest?instId=BTC-USDT-SWAP&instType=SWAP
At bar close, the most recent entry in sel_oi_history is used as OI feature value.
"""

import asyncio
import logging
from datetime import datetime, timezone

import aiohttp

logger = logging.getLogger(__name__)

OKX_OI_URL = "https://www.okx.com/api/v5/public/open-interest"
OKX_FUNDING_URL = "https://www.okx.com/api/v5/public/funding-rate"
PERSIST_INTERVAL_SECS = 300  # 5 minutes


async def _fetch_oi(session: aiohttp.ClientSession, inst_id: str) -> float | None:
    params = {"instId": inst_id, "instType": "SWAP"}
    try:
        async with session.get(
            OKX_OI_URL,
            params=params,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                logger.warning("OI fetch HTTP %s", resp.status)
                return None
            data = await resp.json()
            rows = (data or {}).get("data", [])
            if not rows:
                return None
            item = rows[0]
            # Prefer USD notional; fall back to contract count
            val = item.get("oiUsd") or item.get("oiCcy") or item.get("oi")
            return float(val) if val else None
    except Exception as exc:
        logger.warning("OI fetch error: %s", exc)
        return None


async def _fetch_funding(session: aiohttp.ClientSession, inst_id: str) -> float | None:
    """Current funding rate from OKX (item #19 — funding persister was missing)."""
    try:
        async with session.get(
            OKX_FUNDING_URL,
            params={"instId": inst_id},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                logger.warning("funding fetch HTTP %s", resp.status)
                return None
            data = await resp.json()
            rows = (data or {}).get("data", [])
            if not rows:
                return None
            fr = rows[0].get("fundingRate")
            return float(fr) if fr is not None else None
    except Exception as exc:
        logger.warning("funding fetch error: %s", exc)
        return None


async def run_oi_persister(
    symbol: str = "BTCUSDT",
    inst_id: str = "BTC-USDT-SWAP",
    pool=None,
) -> None:
    """Persist OI snapshot every 5 min to sel_oi_history."""
    if pool is None:
        from shared.db.connections import get_pg

        pool = await get_pg()

    from sel_engine.db.writer import write_funding_snapshot, write_oi_snapshot

    async with aiohttp.ClientSession() as session:
        logger.info("oi_persister started for %s", symbol)
        while True:
            try:
                now = datetime.now(timezone.utc)
                oi = await _fetch_oi(session, inst_id)
                if oi is not None:
                    await write_oi_snapshot(pool, symbol, now, oi)
                    logger.debug("persisted OI %.2f for %s at %s", oi, symbol, now.isoformat())
                # Funding persistence (item #19): sel_funding_history had a writer +
                # table but nothing populated it.
                funding = await _fetch_funding(session, inst_id)
                if funding is not None:
                    await write_funding_snapshot(pool, symbol, now, funding)
                    logger.debug("persisted funding %.6f for %s at %s", funding, symbol, now.isoformat())
            except Exception as exc:
                logger.error("oi_persister error: %s", exc)
            await asyncio.sleep(PERSIST_INTERVAL_SECS)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_oi_persister())
