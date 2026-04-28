"""
Trade flow collector: accumulates taker buy/sell volume per 1H bar from OKX trade feed.
Net taker flow (TF) = taker_buy_vol_USDT - taker_sell_vol_USDT

WIKI_REQUIRED: This collector must be running for TF feature to be non-None.
OKX endpoint: GET /api/v5/market/trades?instId=BTC-USDT-SWAP&limit=100
Poll every ~5s, deduplicate by trade ID stored in Redis set.

Redis key layout:
  sel:tf_accum:{symbol}:{bar_ts_iso}  →  float (net USDT notional, signed)
  sel:tf_trade_ids:{symbol}:{bar_ts_iso}  →  set of seen trade IDs (dedup)
"""
import asyncio
import logging
from datetime import datetime, timezone

import aiohttp

logger = logging.getLogger(__name__)

OKX_TRADES_URL = "https://www.okx.com/api/v5/market/trades"
POLL_INTERVAL_SECS = 5
REDIS_TF_PREFIX = "sel:tf_accum"
REDIS_IDS_PREFIX = "sel:tf_trade_ids"
BAR_EXPIRE_SECS = 7200


def _bar_ts_iso(now: datetime) -> str:
    truncated = now.replace(minute=0, second=0, microsecond=0)
    return truncated.isoformat()


async def _poll_trades(
    session: aiohttp.ClientSession,
    redis,
    inst_id: str,
    symbol: str,
    last_trade_id: list,  # mutable container for state
) -> None:
    params = {"instId": inst_id, "limit": "100"}
    try:
        async with session.get(
            OKX_TRADES_URL,
            params=params,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                return
            data = await resp.json()
            trades = (data or {}).get("data", [])
    except Exception as exc:
        logger.warning("trade_flow poll error: %s", exc)
        return

    if not trades:
        return

    now = datetime.now(timezone.utc)
    bar_ts = _bar_ts_iso(now)
    tf_key = f"{REDIS_TF_PREFIX}:{symbol}:{bar_ts}"
    ids_key = f"{REDIS_IDS_PREFIX}:{symbol}:{bar_ts}"

    for trade in trades:
        trade_id = trade.get("tradeId", "")
        if not trade_id:
            continue
        already_seen = await redis.sismember(ids_key, trade_id)
        if already_seen:
            continue

        await redis.sadd(ids_key, trade_id)
        await redis.expire(ids_key, BAR_EXPIRE_SECS)

        try:
            size = float(trade.get("sz", 0))
            px = float(trade.get("px", 0))
            notional = size * px
            side = trade.get("side", "")
            delta = notional if side == "buy" else -notional
        except (TypeError, ValueError):
            continue

        await redis.incrbyfloat(tf_key, delta)
        await redis.expire(tf_key, BAR_EXPIRE_SECS)


async def run_trade_flow_collector(
    symbol: str = "BTCUSDT",
    inst_id: str = "BTC-USDT-SWAP",
    redis=None,
) -> None:
    """Main loop: poll trades every 5s and accumulate TF per 1H bar."""
    if redis is None:
        from shared.db.connections import get_redis
        redis = await get_redis()

    last_trade_id: list = [None]
    async with aiohttp.ClientSession() as session:
        logger.info("trade_flow_collector started for %s", symbol)
        while True:
            try:
                await _poll_trades(session, redis, inst_id, symbol, last_trade_id)
            except Exception as exc:
                logger.error("trade_flow_collector iteration error: %s", exc)
            await asyncio.sleep(POLL_INTERVAL_SECS)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_trade_flow_collector())
