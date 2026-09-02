"""Feature calculator orchestrator: raw data → FeatureVector."""

import logging
from datetime import datetime
from typing import Optional

from .derived import compute_all_derived
from .flow import get_funding_rate_from_redis
from .liquidity import compute_H_from_samples
from .orderbook import compute_depth_features, compute_depth_features_stub
from .price import compute_price_features
from .schema import FeatureAvailability, FeatureVector

logger = logging.getLogger(__name__)


class FeatureCalculator:
    """
    Orchestrates all feature computation for a single 1H bar.
    Designed to be called at bar close with all available context.
    """

    async def compute(
        self,
        bar_time: datetime,
        symbol: str,
        closes: list[float],
        sigma_p_history: list[float],
        H_history: list[float],
        OI_history: list[float],
        OI_current: Optional[float],
        total_depth_24h_mean: Optional[float],
        spread_bps_24h_mean: Optional[float],
        # Orderbook snapshot (None if collector not running)
        bids: Optional[list[tuple[float, float]]],
        asks: Optional[list[tuple[float, float]]],
        # H samples from Redis for this bar (None if collector not running)
        H_samples: Optional[list[float]],
        # Redis client for funding_rate + TF
        redis=None,
        bar_ts_iso: Optional[str] = None,
        # Rolling TF history maintained by caller (None until caller enables history tracking)
        TF_history: Optional[list] = None,
    ) -> FeatureVector:
        fv = FeatureVector(time=bar_time, symbol=symbol)
        avail = FeatureAvailability()

        # --- Price layer ---
        price_feats = compute_price_features(closes)
        fv.close = price_feats.get("close", 0.0)
        fv.delta_p_pct = price_feats.get("delta_p_pct")
        fv.sigma_p_24h = price_feats.get("sigma_p_24h")
        avail.close = True
        avail.delta_p_pct = fv.delta_p_pct is not None
        avail.sigma_p_24h = fv.sigma_p_24h is not None

        # --- Liquidity / H ---
        if H_samples is not None:
            fv.H, fv.H_sample_count = compute_H_from_samples(H_samples)
            avail.H = fv.H is not None
            if fv.H is not None and H_history and H_history[-1] is not None:
                fv.delta_H = abs(fv.H - H_history[-1])
                avail.delta_H = True
        # else H remains None per WIKI_REQUIRED in liquidity.py

        # --- Orderbook depth ---
        if bids is not None and asks is not None:
            depth = compute_depth_features(bids, asks)
        else:
            depth = compute_depth_features_stub()
        fv.top_5_bid_size = depth["top_5_bid_size"]
        fv.top_5_ask_size = depth["top_5_ask_size"]
        fv.total_depth = depth["total_depth"]
        fv.spread_bps = depth["spread_bps"]
        avail.top_5_bid_size = fv.top_5_bid_size is not None
        avail.top_5_ask_size = fv.top_5_ask_size is not None
        avail.total_depth = fv.total_depth is not None
        avail.spread_bps = fv.spread_bps is not None

        # --- Flow layer ---
        if redis is not None and bar_ts_iso is not None:
            from .flow import get_tf_from_redis

            fv.TF = await get_tf_from_redis(redis, symbol, bar_ts_iso)
            fv.funding_rate = await get_funding_rate_from_redis(redis, symbol)
        avail.TF = fv.TF is not None
        avail.funding_rate = fv.funding_rate is not None

        fv.OI = OI_current
        avail.OI = fv.OI is not None

        # --- Derived indicators ---
        derived = compute_all_derived(
            closes=closes,
            sigma_p_history=sigma_p_history,
            H_history=H_history,
            OI_history=OI_history,
            total_depth=fv.total_depth,
            total_depth_24h_mean=total_depth_24h_mean,
            spread_bps=fv.spread_bps,
            spread_bps_24h_mean=spread_bps_24h_mean,
            TF=fv.TF,
            delta_p_pct=fv.delta_p_pct,
            TF_history=TF_history,
        )
        fv.LV = derived["LV"]
        fv.absorption_ratio = derived["absorption_ratio"]
        fv.price_autocorr_12h = derived["price_autocorr_12h"]
        fv.price_autocorr_24h = derived["price_autocorr_24h"]
        fv.price_autocorr_48h = derived["price_autocorr_48h"]
        fv.sigma_p_d2 = derived["sigma_p_d2"]
        fv.H_change_rate_std_12h = derived["H_change_rate_std_12h"]
        fv.OI_hurst = derived["OI_hurst"]
        # P1 features
        fv.oi_change_rate_24h = derived["oi_change_rate_24h"]
        fv.H_24h_mean = derived["H_24h_mean"]
        fv.tf_dp_ratio_24h = derived["tf_dp_ratio_24h"]
        fv.tf_directional_ratio_6h = derived["tf_directional_ratio_6h"]
        fv.abs_tf_24h_sum = derived["abs_tf_24h_sum"]
        fv.price_slope_6h = derived["price_slope_6h"]
        fv.sigma_rising_12h = derived["sigma_rising_12h"]
        fv.sigma_change_rate_std_6h = derived["sigma_change_rate_std_6h"]

        avail.LV = fv.LV is not None
        avail.absorption_ratio = fv.absorption_ratio is not None
        avail.price_autocorr_12h = fv.price_autocorr_12h is not None
        avail.price_autocorr_24h = fv.price_autocorr_24h is not None
        avail.price_autocorr_48h = fv.price_autocorr_48h is not None
        avail.sigma_p_d2 = fv.sigma_p_d2 is not None
        avail.H_change_rate_std_12h = fv.H_change_rate_std_12h is not None
        avail.OI_hurst = fv.OI_hurst is not None
        avail.oi_change_rate_24h = fv.oi_change_rate_24h is not None
        avail.H_24h_mean = fv.H_24h_mean is not None
        avail.tf_dp_ratio_24h = fv.tf_dp_ratio_24h is not None
        avail.tf_directional_ratio_6h = fv.tf_directional_ratio_6h is not None
        avail.abs_tf_24h_sum = fv.abs_tf_24h_sum is not None
        avail.price_slope_6h = fv.price_slope_6h is not None
        avail.sigma_rising_12h = fv.sigma_rising_12h is not None
        avail.sigma_change_rate_std_6h = fv.sigma_change_rate_std_6h is not None

        fv.availability = avail
        return fv
