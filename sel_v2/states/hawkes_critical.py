"""
H2 rolling Hawkes branching ratio estimator for Critical state main condition B.
v2.1 §5.2 — Hawkes branching ratio > threshold.

Per 4H bar close:
  - Take last 540 bars (90 days × 6 bars/day) of price data
  - Event = |4H log-return| > 1σ of full window returns
  - Fit exponential Hawkes MLE → branching ratio α/β
  - Compare with threshold from v2_strategy_params

Relation to H1 (Wave 2):
  H1 = per-event second-scale intensity tracker for Strategy 2 entry
  H2 = 4H-bar scale rolling MLE for state machine Critical condition
  These are independent; shared math lives in sel_v2/hawkes/mle.py.
"""
from __future__ import annotations

import logging
from typing import Optional

import numpy as np

from sel_v2.hawkes.mle import fit_hawkes

logger = logging.getLogger(__name__)

_WINDOW_BARS = 540    # 90 days × 6 bars/day
_EVENT_SIGMA = 1.0    # event = |return| > 1σ
_MAX_BR = 10.0        # clip optimizer overflow


def compute_hawkes_branching_ratio(
    log_returns: np.ndarray,
    window: int = _WINDOW_BARS,
    event_sigma_multiplier: float = _EVENT_SIGMA,
    n_restarts: int = 3,
) -> Optional[float]:
    """
    Compute Hawkes branching ratio from the trailing `window` log-returns.

    Args:
        log_returns: 1D array of log-returns aligned to bar closes.
                     The LAST element corresponds to the most recent bar.
        window:      How many bars to look back (default 540 = 90 days).
        event_sigma_multiplier: event threshold in σ units.
        n_restarts:  MLE random restarts.

    Returns:
        Branching ratio α/β, or None if insufficient data or failed convergence.
    """
    if len(log_returns) < max(window, 50):
        return None

    seg = log_returns[-window:]
    sigma_seg = float(np.std(seg))
    if sigma_seg <= 0:
        return None

    threshold = event_sigma_multiplier * sigma_seg
    event_flags = np.abs(seg) > threshold
    event_times = np.where(event_flags)[0].astype(float)

    if len(event_times) < 5:
        return None

    T = float(len(seg) - 1)
    result = fit_hawkes(event_times, T, n_restarts=n_restarts)

    if not result.get("converged") and np.isnan(result.get("branching_ratio", float("nan"))):
        return None

    br = result.get("branching_ratio", float("nan"))
    if not np.isfinite(br):
        return None

    return float(np.clip(br, 0.0, _MAX_BR))


def precompute_branching_ratios(
    close_prices: np.ndarray,
    window: int = _WINDOW_BARS,
    n_restarts: int = 3,
) -> np.ndarray:
    """
    Precompute rolling branching ratios for all bars.
    Used by the offline replay to avoid recomputing per-bar.

    Returns array of shape (n_bars,) with NaN before warmup.
    """
    n = len(close_prices)
    log_returns = np.diff(np.log(close_prices))  # length n-1
    br_series = np.full(n, np.nan)

    for i in range(window, n):
        # log_returns[i-window : i] covers bars close[i-window] → close[i]
        seg_returns = log_returns[i - window: i]
        br = compute_hawkes_branching_ratio(
            seg_returns, window=window, n_restarts=n_restarts
        )
        br_series[i] = br if br is not None else np.nan
        if i % 200 == 0:
            logger.debug("Hawkes BR computed at bar %d / %d  br=%.4f", i, n, br_series[i])

    return br_series
