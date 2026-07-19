"""Derived indicators: LV, OI_hurst, and orchestration of all derived features."""

import logging
from typing import Optional

import numpy as np

from .liquidity import compute_H_change_rate_std
from .price import (
    compute_autocorr,
    compute_sigma_p_d2,
    compute_price_slope_6h,
    compute_sigma_slope_12h,
    compute_sigma_change_rate_std_6h,
)

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
    if any(
        v is None
        for v in (total_depth, total_depth_24h_mean, spread_bps, spread_bps_24h_mean)
    ):
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
    H ≈ 0.5 → random walk; > 0.5 → trending; < 0.5 → mean-reverting.
    Returns None if series is too short or degenerate.
    """
    if len(series) < 20:
        return None
    # oprim is a REQUIRED dependency, so import it OUTSIDE the try: a missing or
    # broken install must raise, not degrade every Hurst reading to None for the
    # lifetime of the process. The try still guards numerical failures below.
    # (`except (ValueError, Exception)` was redundant — ValueError ⊂ Exception.)
    from oprim import hurst_exponent

    try:
        return hurst_exponent(np.array(series, dtype=float), min_window=4)
    except Exception:
        return None


def compute_oi_change_rate_24h(OI_history: list) -> Optional[float]:
    """
    OI 24H cumulative change rate = (OI_now - OI_24h_ago) / |OI_24h_ago|.
    Returns None when fewer than 25 OI values are available or denominator is zero.
    doc §4.1 Cond3; also used in §4.3 Cond4.
    """
    if len(OI_history) < 25:
        return None
    oi_now = OI_history[-1]
    oi_24h = OI_history[-25]
    if oi_now is None or oi_24h is None or oi_24h == 0:
        return None
    return float((oi_now - oi_24h) / abs(oi_24h))


def compute_H_24h_mean(H_history: list) -> Optional[float]:
    """
    Mean of H entropy over the last 24 bars.
    Requires at least 12 valid (non-None, finite) values out of the 24-bar window.
    doc §4.3 Cond2; §4.4 Cond2.
    """
    if not H_history:
        return None
    valid = [h for h in H_history[-24:] if h is not None and np.isfinite(h)]
    if len(valid) < 12:
        return None
    return float(np.mean(valid))


def compute_tf_dp_ratio_24h(
    TF_history: Optional[list],
    closes: list[float],
) -> Optional[float]:
    """
    |TF| 24H cumulative / |ΔP| 24H cumulative (doc §4.1 Cond4 Absorption proxy).
    Returns None if TF_history unavailable, or cumulative |ΔP| is zero.
    """
    if TF_history is None or len(TF_history) < 24:
        return None
    if len(closes) < 25:
        return None
    tf_24 = TF_history[-24:]
    sum_abs_tf = sum(abs(t) for t in tf_24 if t is not None)
    prices_25 = closes[-25:]
    sum_abs_dp = sum(abs(prices_25[i + 1] - prices_25[i]) for i in range(24))
    if sum_abs_dp == 0.0:
        return None
    return float(sum_abs_tf / sum_abs_dp)


def compute_tf_directional_ratio_6h(
    TF_history: Optional[list],
) -> Optional[float]:
    """
    Signed directional ratio over last 6 bars: (count_pos - count_neg) / count_valid.
    Range [-1, 1].  Positive → net-buy direction (Surging_Up), negative → Surging_Down.
    Returns None when TF_history unavailable or all bars are zero-TF.
    doc §4.2 Cond2 + direction signal.
    """
    if TF_history is None or len(TF_history) < 6:
        return None
    tf_6 = [t for t in TF_history[-6:] if t is not None]
    if not tf_6:
        return None
    pos = sum(1 for t in tf_6 if t > 0)
    neg = sum(1 for t in tf_6 if t < 0)
    total = len(tf_6)
    return float(pos - neg) / total


def compute_abs_tf_24h_sum(TF_history: Optional[list]) -> Optional[float]:
    """
    Sum of |TF| over the last 24 bars.
    Returns None when TF_history unavailable or all TF values are None.
    doc §4.3 Cond3; §4.4 Cond3.
    """
    if TF_history is None or len(TF_history) < 24:
        return None
    tf_24 = [t for t in TF_history[-24:] if t is not None]
    if not tf_24:
        return None
    return float(sum(abs(t) for t in tf_24))


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
    TF_history: Optional[list] = None,
) -> dict:
    """Compute all derived indicators from their input series. Returns dict of values."""
    from .flow import compute_absorption_ratio

    return {
        "LV": compute_LV(
            total_depth, total_depth_24h_mean, spread_bps, spread_bps_24h_mean
        ),
        "absorption_ratio": compute_absorption_ratio(TF, delta_p_pct),
        "price_autocorr_12h": compute_autocorr(closes, 12),
        "price_autocorr_24h": compute_autocorr(closes, 24),
        "price_autocorr_48h": compute_autocorr(closes, 48),
        "sigma_p_d2": compute_sigma_p_d2(sigma_p_history),
        "H_change_rate_std_12h": compute_H_change_rate_std(H_history, window=12),
        "OI_hurst": compute_hurst_rs(OI_history),
        # P1 derived features (doc §4.1–4.4)
        "oi_change_rate_24h": compute_oi_change_rate_24h(OI_history),
        "H_24h_mean": compute_H_24h_mean(H_history),
        "tf_dp_ratio_24h": compute_tf_dp_ratio_24h(TF_history, closes),
        "tf_directional_ratio_6h": compute_tf_directional_ratio_6h(TF_history),
        "abs_tf_24h_sum": compute_abs_tf_24h_sum(TF_history),
        "price_slope_6h": compute_price_slope_6h(closes),
        "sigma_rising_12h": compute_sigma_slope_12h(sigma_p_history),
        "sigma_change_rate_std_6h": compute_sigma_change_rate_std_6h(sigma_p_history),
    }
