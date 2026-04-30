"""Flow layer: TF (taker flow), OI (open interest), funding_rate."""
import json
import logging
from typing import Optional

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

# WIKI_REQUIRED: TF requires trade_flow_collector running.
# OKX endpoint: GET /api/v5/market/trades?instId=BTC-USDT-SWAP&limit=100
# Poll every ~5s, accumulate taker_buy_vol - taker_sell_vol in USDT notional.
# Redis key: sel:tf_accum:{symbol}:{bar_ts_iso}  (reset at bar close).
# Until deployed, TF is always None.

# OI: oi_persister reads from OKX /api/v5/public/open-interest every 5min
# and writes to sel_oi_history table. At bar close the latest value is used.

REDIS_FUNDING_KEY_PREFIX = "cw4:funding_rates"


async def get_funding_rate_from_redis(
    redis: aioredis.Redis, symbol: str
) -> Optional[float]:
    """Read current funding rate from Redis hash written by the data service."""
    try:
        raw = await redis.hget(REDIS_FUNDING_KEY_PREFIX, symbol)
        if raw is None:
            return None
        decoded = raw.decode() if isinstance(raw, bytes) else raw
        try:
            parsed = json.loads(decoded)
            return float(parsed["rate"]) if isinstance(parsed, dict) else float(parsed)
        except (json.JSONDecodeError, KeyError, TypeError):
            return float(decoded)
    except Exception as exc:
        logger.warning("funding_rate redis read failed for %s: %s", symbol, exc)
        return None


async def get_tf_from_redis(
    redis: aioredis.Redis, symbol: str, bar_ts_iso: str
) -> Optional[float]:
    """
    Read accumulated taker flow for the closing bar.
    # WIKI_REQUIRED: trade_flow_collector must write sel:tf_accum:{symbol}:{bar_ts_iso}
    """
    key = f"sel:tf_accum:{symbol}:{bar_ts_iso}"
    try:
        raw = await redis.get(key)
        if raw is None:
            return None
        return float(raw)
    except Exception as exc:
        logger.warning("TF redis read failed for %s/%s: %s", symbol, bar_ts_iso, exc)
        return None


def compute_absorption_ratio(
    TF: Optional[float], delta_p_pct: Optional[float]
) -> Optional[float]:
    """
    absorption_ratio = |TF| / |ΔP|.
    High value means large flow but little price movement (absorbed by passive liquidity).
    Returns None when TF is missing or ΔP is zero/None.
    """
    if TF is None or delta_p_pct is None:
        return None
    if delta_p_pct == 0.0:
        return None
    return abs(TF) / abs(delta_p_pct)
