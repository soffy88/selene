"""Persist FeatureVector to sel_features hypertable via upsert."""
import dataclasses
import json
import logging
from typing import Optional

import asyncpg

from sel_engine.features.schema import FeatureVector

logger = logging.getLogger(__name__)

_UPSERT_SQL = """
INSERT INTO sel_features (
    time, symbol,
    close, delta_p_pct, sigma_p_24h,
    H, H_sample_count,
    top_5_bid_size, top_5_ask_size, total_depth, spread_bps,
    TF, OI, funding_rate,
    LV, absorption_ratio,
    price_autocorr_12h, price_autocorr_24h, price_autocorr_48h,
    sigma_p_d2, H_change_rate_std_12h, OI_hurst,
    data_quality, computed_at
) VALUES (
    $1, $2,
    $3, $4, $5,
    $6, $7,
    $8, $9, $10, $11,
    $12, $13, $14,
    $15, $16,
    $17, $18, $19,
    $20, $21, $22,
    $23, NOW()
)
ON CONFLICT (time, symbol) DO UPDATE SET
    close               = EXCLUDED.close,
    delta_p_pct         = EXCLUDED.delta_p_pct,
    sigma_p_24h         = EXCLUDED.sigma_p_24h,
    H                   = EXCLUDED.H,
    H_sample_count      = EXCLUDED.H_sample_count,
    top_5_bid_size      = EXCLUDED.top_5_bid_size,
    top_5_ask_size      = EXCLUDED.top_5_ask_size,
    total_depth         = EXCLUDED.total_depth,
    spread_bps          = EXCLUDED.spread_bps,
    TF                  = EXCLUDED.TF,
    OI                  = EXCLUDED.OI,
    funding_rate        = EXCLUDED.funding_rate,
    LV                  = EXCLUDED.LV,
    absorption_ratio    = EXCLUDED.absorption_ratio,
    price_autocorr_12h  = EXCLUDED.price_autocorr_12h,
    price_autocorr_24h  = EXCLUDED.price_autocorr_24h,
    price_autocorr_48h  = EXCLUDED.price_autocorr_48h,
    sigma_p_d2          = EXCLUDED.sigma_p_d2,
    H_change_rate_std_12h = EXCLUDED.H_change_rate_std_12h,
    OI_hurst            = EXCLUDED.OI_hurst,
    data_quality        = EXCLUDED.data_quality,
    computed_at         = NOW()
"""


async def write_feature_vector(pool: asyncpg.Pool, fv: FeatureVector) -> None:
    avail_dict = dataclasses.asdict(fv.availability)
    data_quality = json.dumps(avail_dict)

    params = (
        fv.time, fv.symbol,
        fv.close, fv.delta_p_pct, fv.sigma_p_24h,
        fv.H, fv.H_sample_count,
        fv.top_5_bid_size, fv.top_5_ask_size, fv.total_depth, fv.spread_bps,
        fv.TF, fv.OI, fv.funding_rate,
        fv.LV, fv.absorption_ratio,
        fv.price_autocorr_12h, fv.price_autocorr_24h, fv.price_autocorr_48h,
        fv.sigma_p_d2, fv.H_change_rate_std_12h, fv.OI_hurst,
        data_quality,
    )
    async with pool.acquire() as conn:
        await conn.execute(_UPSERT_SQL, *params)
    logger.debug("wrote sel_features row: %s %s", fv.symbol, fv.time.isoformat())


async def write_oi_snapshot(
    pool: asyncpg.Pool, symbol: str, ts, oi_value: float
) -> None:
    sql = """
        INSERT INTO sel_oi_history (time, symbol, oi_value)
        VALUES ($1, $2, $3)
        ON CONFLICT (time, symbol) DO UPDATE SET oi_value = EXCLUDED.oi_value
    """
    async with pool.acquire() as conn:
        await conn.execute(sql, ts, symbol, oi_value)


async def write_funding_snapshot(
    pool: asyncpg.Pool, symbol: str, ts, funding_rate: float
) -> None:
    sql = """
        INSERT INTO sel_funding_history (time, symbol, funding_rate)
        VALUES ($1, $2, $3)
        ON CONFLICT (time, symbol) DO UPDATE SET funding_rate = EXCLUDED.funding_rate
    """
    async with pool.acquire() as conn:
        await conn.execute(sql, ts, symbol, funding_rate)


async def write_orderbook_sample(
    pool: asyncpg.Pool,
    symbol: str,
    time_bucket,
    H_sample: Optional[float],
    top_5_bid: Optional[float],
    top_5_ask: Optional[float],
    spread_bps: Optional[float],
) -> None:
    # WIKI_REQUIRED: called by orderbook_collector once deployed
    sql = """
        INSERT INTO sel_orderbook_samples
            (time_bucket, symbol, H_sample, top_5_bid, top_5_ask, spread_bps)
        VALUES ($1, $2, $3, $4, $5, $6)
    """
    async with pool.acquire() as conn:
        await conn.execute(sql, time_bucket, symbol, H_sample, top_5_bid, top_5_ask, spread_bps)
