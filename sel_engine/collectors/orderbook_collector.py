"""
Orderbook collector: samples OKX order book every 60s, computes H and depth features,
stores per-bar samples in Redis and persists 5-min buckets to TimescaleDB.

WIKI_REQUIRED: This collector must be running for H, top_5_bid_size, top_5_ask_size,
total_depth, and spread_bps features to be populated.

OKX endpoint: GET /api/v5/market/books?instId=BTC-USDT-SWAP&sz=20
"""
import asyncio
import json
import logging
import os
from datetime import datetime, timezone

import aiohttp

from sel_engine.features.liquidity import compute_orderbook_entropy
from sel_engine.features.orderbook import compute_depth_features

logger = logging.getLogger(__name__)

OKX_BOOKS_URL = "https://www.okx.com/api/v5/market/books"
SAMPLE_INTERVAL_SECS = 60
REDIS_H_SAMPLES_PREFIX = "sel:h_samples"
REDIS_DEPTH_SAMPLES_PREFIX = "sel:depth_samples"
BAR_EXPIRE_SECS = 7200  # keep 2 bars of samples in Redis


def _bar_ts_iso(now: datetime) -> str:
    """Truncate to current 1H bar start."""
    truncated = now.replace(minute=0, second=0, microsecond=0)
    return truncated.isoformat()


async def _fetch_orderbook(session: aiohttp.ClientSession, inst_id: str) -> dict | None:
    params = {"instId": inst_id, "sz": "20"}
    try:
        async with session.get(
            OKX_BOOKS_URL,
            params=params,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                logger.warning("orderbook HTTP %s for %s", resp.status, inst_id)
                return None
            data = await resp.json()
            books = (data or {}).get("data", [])
            return books[0] if books else None
    except Exception as exc:
        logger.warning("orderbook fetch error: %s", exc)
        return None


async def _sample_once(
    session: aiohttp.ClientSession,
    redis,
    pool,
    inst_id: str,
    symbol: str,
) -> None:
    book = await _fetch_orderbook(session, inst_id)
    if not book:
        return

    now = datetime.now(timezone.utc)
    bar_ts = _bar_ts_iso(now)

    bids = [(float(p), float(s)) for p, s, *_ in book.get("bids", [])]
    asks = [(float(p), float(s)) for p, s, *_ in book.get("asks", [])]

    H = compute_orderbook_entropy(bids)
    depth = compute_depth_features(bids, asks)

    sample_payload = json.dumps({
        "H": H,
        "top_5_bid": depth["top_5_bid_size"],
        "top_5_ask": depth["top_5_ask_size"],
        "spread_bps": depth["spread_bps"],
        "ts": now.isoformat(),
    })

    h_key = f"{REDIS_H_SAMPLES_PREFIX}:{symbol}:{bar_ts}"
    depth_key = f"{REDIS_DEPTH_SAMPLES_PREFIX}:{symbol}:{bar_ts}"

    await redis.rpush(h_key, str(H))
    await redis.expire(h_key, BAR_EXPIRE_SECS)
    await redis.rpush(depth_key, sample_payload)
    await redis.expire(depth_key, BAR_EXPIRE_SECS)

    # Persist 5-min bucket to DB if pool available
    if pool is not None:
        from sel_engine.db.writer import write_orderbook_sample
        bucket = now.replace(minute=(now.minute // 5) * 5, second=0, microsecond=0)
        try:
            await write_orderbook_sample(
                pool, symbol, bucket, H,
                depth["top_5_bid_size"], depth["top_5_ask_size"], depth["spread_bps"],
            )
        except Exception as exc:
            logger.warning("orderbook sample DB write failed: %s", exc)


async def run_orderbook_collector(
    symbol: str = "BTCUSDT",
    inst_id: str = "BTC-USDT-SWAP",
    redis=None,
    pool=None,
) -> None:
    """Main loop: sample orderbook every 60s indefinitely."""
    if redis is None:
        from shared.db.connections import get_redis
        redis = await get_redis()
    if pool is None:
        from shared.db.connections import get_pg
        pool = await get_pg()

    async with aiohttp.ClientSession() as session:
        logger.info("orderbook_collector started for %s", symbol)
        while True:
            try:
                await _sample_once(session, redis, pool, inst_id, symbol)
            except Exception as exc:
                logger.error("orderbook_collector iteration error: %s", exc)
            await asyncio.sleep(SAMPLE_INTERVAL_SECS)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_orderbook_collector())
