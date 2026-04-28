"""Price layer feature computations: close, delta_p_pct, sigma_p_24h, autocorr, sigma_p_d2."""
import logging
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


def compute_price_features(closes: list[float]) -> dict:
    """
    closes: oldest-first list of closing prices; closes[-1] is the current bar.
    Returns dict with keys: close, delta_p_pct, sigma_p_24h.
    """
    if not closes:
        return {}

    close = closes[-1]

    delta_p_pct = None
    if len(closes) >= 2 and closes[-2] != 0:
        delta_p_pct = (closes[-1] - closes[-2]) / closes[-2] * 100.0

    sigma_p_24h = None
    if len(closes) >= 25:
        window = closes[-25:]
        log_returns = np.diff(np.log(window))
        sigma_p_24h = float(np.std(log_returns, ddof=1))

    return {"close": close, "delta_p_pct": delta_p_pct, "sigma_p_24h": sigma_p_24h}


def compute_autocorr(closes: list[float], window: int) -> Optional[float]:
    """Lag-1 autocorrelation of log returns over the most recent `window` bars."""
    if len(closes) < window + 1:
        return None
    prices = np.array(closes[-(window + 1):], dtype=float)
    returns = np.diff(np.log(prices))
    if len(returns) < 2:
        return None
    # corrcoef returns NaN for zero-variance series; guard against that.
    try:
        val = float(np.corrcoef(returns[:-1], returns[1:])[0, 1])
        if np.isnan(val):
            return None
        return val
    except Exception as exc:
        logger.debug("autocorr computation failed: %s", exc)
        return None


def compute_sigma_p_d2(sigma_history: list[float]) -> Optional[float]:
    """Second difference of sigma_p: σ[t] - 2*σ[t-1] + σ[t-2]. Captures acceleration of vol."""
    if len(sigma_history) < 3:
        return None
    return float(sigma_history[-1] - 2 * sigma_history[-2] + sigma_history[-3])


def compute_price_slope_6h(closes: list[float]) -> Optional[float]:
    """
    Absolute linear regression slope over the most recent 6H (6 one-bar intervals, 7 closes).
    Normalized by mean close price to yield a dimensionless rate (doc §4.2 Cond1).
    Returns None when fewer than 7 closes are available.
    """
    if len(closes) < 7:
        return None
    window = np.array(closes[-7:], dtype=float)
    mean_price = float(window.mean())
    if mean_price == 0.0:
        return None
    x = np.arange(6, dtype=float)
    y = window[1:]  # 6 log-prices corresponding to intervals
    slope = float(np.polyfit(x, y, 1)[0])
    return abs(slope) / mean_price


def compute_sigma_slope_12h(sigma_history: list[float]) -> Optional[bool]:
    """True if current sigma_p is strictly higher than sigma_p 12H ago (sigma rising, doc §4.2 Cond3)."""
    if len(sigma_history) < 13:
        return None
    return bool(sigma_history[-1] > sigma_history[-13])


def compute_sigma_change_rate_std_6h(sigma_history: list[float]) -> Optional[float]:
    """Std of sigma_p first-differences over the last 6 bars (doc §4.2 Cond3 stability check)."""
    if len(sigma_history) < 7:
        return None
    window = np.array(sigma_history[-7:], dtype=float)
    diffs = np.diff(window)  # 6 differences
    if len(diffs) < 2:
        return None
    return float(np.std(diffs, ddof=1))
