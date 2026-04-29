"""
TDA1 realtime Persistence Landscape L^1 norm estimator.
v2.1 §12.1 — Critical state main condition C.

Per 4H bar close:
  - Take last 50 bars of log-prices (v2.1 spec: W=50-100)
  - Takens embedding d=4 τ=1 (v2.1 spec)
  - Vietoris-Rips persistent homology (ripser)
  - Persistence Landscape H1 L^1 norm
  - Rolling 90-day 95th-percentile threshold from v2_strategy_params

Bug fixes inherited from Wave 1 tda_calibration.py:
  1. Use LOG-PRICES (not log-returns) for Takens embedding
     → log-prices create meaningful orbits in phase space (Gidea & Katz 2018)
  2. Use np.trapezoid (not deprecated np.trapz) for L^1 norm integration
"""
from __future__ import annotations

import logging
from typing import Optional

import numpy as np
from ripser import ripser

logger = logging.getLogger(__name__)

_WINDOW_BARS = 50   # bars per computation
_EMBED_DIM = 4      # Takens embedding dimension
_EMBED_TAU = 1      # Takens time delay (bars)
_MAX_CLOUD = 200    # subsample if point cloud exceeds this


# ── Core TDA computation (re-uses Wave 1 algorithm exactly) ──────────────────


def takens_embed(x: np.ndarray, d: int = _EMBED_DIM, tau: int = _EMBED_TAU) -> np.ndarray:
    """Takens delay embedding. x is 1D, returns (N-(d-1)*tau) × d matrix."""
    n = len(x) - (d - 1) * tau
    if n <= 0:
        raise ValueError(f"Series too short: len={len(x)}, d={d}, tau={tau}")
    return np.stack([x[i * tau: i * tau + n] for i in range(d)], axis=1)


def _persistence_landscape(dgm: np.ndarray, resolution: int = 100, x_max: float = 2.0) -> np.ndarray:
    """Convert H1 persistence diagram to L^1 landscape (k=1)."""
    if len(dgm) == 0:
        return np.zeros(resolution)
    t_vals = np.linspace(0.0, x_max, resolution)
    tents = np.zeros((len(dgm), resolution))
    for i, (b, d) in enumerate(dgm):
        if not np.isfinite(d):
            d = x_max
        tents[i] = np.maximum(0, np.minimum(t_vals - b, d - t_vals))
    tents_sorted = np.sort(tents, axis=0)[::-1]
    return tents_sorted[0] if len(tents_sorted) > 0 else np.zeros(resolution)


def compute_pl_l1(
    log_prices: np.ndarray,
    d: int = _EMBED_DIM,
    tau: int = _EMBED_TAU,
    resolution: int = 100,
) -> float:
    """
    Compute Persistence Landscape L^1 norm from a window of LOG-PRICES.

    Uses log-prices (not returns) per Wave 1 bug fix #1.
    Uses np.trapezoid per Wave 1 bug fix #2.
    """
    try:
        cloud = takens_embed(log_prices, d=d, tau=tau)
        if len(cloud) < 5:
            return 0.0

        if len(cloud) > _MAX_CLOUD:
            rng = np.random.default_rng(0)
            idx = rng.choice(len(cloud), _MAX_CLOUD, replace=False)
            cloud = cloud[idx]

        dgms = ripser(cloud, maxdim=1)["dgms"]
        dgm_h1 = dgms[1] if len(dgms) > 1 else np.empty((0, 2))

        if len(dgm_h1) == 0:
            return 0.0

        finite_mask = np.isfinite(dgm_h1[:, 1])
        dgm_finite = dgm_h1[finite_mask]
        if len(dgm_finite) == 0:
            return 0.0

        x_max = float(dgm_finite[:, 1].max()) * 1.1
        pl = _persistence_landscape(dgm_finite, resolution=resolution, x_max=x_max)
        # Wave 1 bug fix #2: use np.trapezoid
        l1_norm = float(np.trapezoid(np.abs(pl), np.linspace(0.0, x_max, resolution)))
        return l1_norm

    except Exception as exc:
        logger.debug("TDA computation error: %s", exc)
        return 0.0


def precompute_tda_l1(
    close_prices: np.ndarray,
    window: int = _WINDOW_BARS,
) -> np.ndarray:
    """
    Precompute rolling TDA L^1 norms for all bars.

    Args:
        close_prices: array of bar closes.
        window: sliding window in bars.

    Returns:
        Array shape (n_bars,) with NaN before warmup.
    """
    n = len(close_prices)
    log_prices = np.log(close_prices)
    l1_series = np.full(n, np.nan)

    for i in range(window, n):
        seg = log_prices[i - window: i]
        l1_series[i] = compute_pl_l1(seg)
        if i % 500 == 0:
            logger.debug("TDA L^1 computed at bar %d / %d  l1=%.6f", i, n, l1_series[i])

    return l1_series


def compute_tda_rolling_pctile(
    l1_series: np.ndarray,
    quantile_window: int = 540,
    q: float = 0.95,
) -> np.ndarray:
    """
    Compute rolling q-th percentile of l1_series over quantile_window bars.
    Used as the dynamic threshold for TDA condition C.
    """
    n = len(l1_series)
    pctile = np.full(n, np.nan)

    for i in range(quantile_window, n):
        seg = l1_series[i - quantile_window: i]
        valid = seg[np.isfinite(seg)]
        if len(valid) >= 10:
            pctile[i] = float(np.quantile(valid, q))

    return pctile
