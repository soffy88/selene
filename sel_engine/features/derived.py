"""Derived indicators: LV, OI_hurst, and orchestration of all derived features."""
import logging
from typing import Optional

import numpy as np

from .liquidity import compute_H_change_rate_std
from .price import compute_autocorr, compute_sigma_p_d2

logger = logging.getLogger(__name__)


def compute_LV(
    total_depth: Optional[float],
    total_depth_24h_mean: Optional[float],
    spread_bps: Optional[float],
    spread_bps_24h_mean: Optional[float],
) -> Optional[float]:
    """
    LV composite [0, 1].
    depth_drop_score = max(0, (mean_depth - curr_depth) / mean_depth)
    spread_expansion_score = max(0, curr_spread / mean_spread - 1)   clipped to [0, 2]
    lv = depth_score * 0.5 + spread_score * 0.25
    Both inputs must be available; returns None otherwise.
    """
    if any(v is None for v in (total_depth, total_depth_24h_mean, spread_bps, spread_bps_24h_mean)):
        return None
    if total_depth_24h_mean <= 0 or spread_bps_24h_mean <= 0:
        return None

    depth_drop = (total_depth_24h_mean - total_depth) / total_depth_24h_mean
    depth_score = max(0.0, float(depth_drop))

    spread_ratio = spread_bps / spread_bps_24h_mean
    spread_score = max(0.0, float(spread_ratio) - 1.0)

    lv = depth_score * 0.5 + min(spread_score, 2.0) * 0.25
    return float(min(1.0, lv))


def compute_hurst_rs(series: list[float]) -> Optional[float]:
    """
    Hurst exponent via R/S analysis.
    For each lag size, average R/S across all non-overlapping sub-windows;
    regress log(mean_RS) on log(lag) to get H.
    H ≈ 0.5 → random walk; > 0.5 → trending; < 0.5 → mean-reverting.
    Returns None if series is too short or degenerate.
    """
    if len(series) < 20:
        return None

    arr = np.array(series, dtype=float)
    n = len(arr)

    lags = [4, 8, 16, min(32, n // 2)]
    lags = [l for l in lags if 2 <= l <= n // 2]
    if len(lags) < 2:
        return None

    rs_means: list[float] = []
    used_lags: list[int] = []
    for lag in lags:
        rs_sub: list[float] = []
        # Average over all non-overlapping sub-windows of length `lag`
        num_windows = n // lag
        for w in range(num_windows):
            sub = arr[w * lag: (w + 1) * lag]
            mean = sub.mean()
            dev = sub - mean
            cumdev = np.cumsum(dev)
            R = float(cumdev.max() - cumdev.min())
            S = float(sub.std(ddof=1))
            if S > 0 and R > 0:
                rs_sub.append(R / S)
        if rs_sub:
            rs_means.append(float(np.mean(rs_sub)))
            used_lags.append(lag)

    if len(rs_means) < 2:
        return None

    log_lags = np.log(np.array(used_lags, dtype=float))
    log_rs = np.log(np.array(rs_means, dtype=float))
    hurst = float(np.polyfit(log_lags, log_rs, 1)[0])
    return max(0.0, min(1.0, hurst))


def compute_all_derived(
    closes: list[float],
    sigma_p_history: list[float],
    H_history: list[float],
    OI_history: list[float],
    total_depth: Optional[float],
    total_depth_24h_mean: Optional[float],
    spread_bps: Optional[float],
    spread_bps_24h_mean: Optional[float],
    TF: Optional[float],
    delta_p_pct: Optional[float],
) -> dict:
    """Compute all derived indicators from their input series. Returns dict of values."""
    from .flow import compute_absorption_ratio

    return {
        "LV": compute_LV(total_depth, total_depth_24h_mean, spread_bps, spread_bps_24h_mean),
        "absorption_ratio": compute_absorption_ratio(TF, delta_p_pct),
        "price_autocorr_12h": compute_autocorr(closes, 12),
        "price_autocorr_24h": compute_autocorr(closes, 24),
        "price_autocorr_48h": compute_autocorr(closes, 48),
        "sigma_p_d2": compute_sigma_p_d2(sigma_p_history),
        "H_change_rate_std_12h": compute_H_change_rate_std(H_history, window=12),
        "OI_hurst": compute_hurst_rs(OI_history),
    }
