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
