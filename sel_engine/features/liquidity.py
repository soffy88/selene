"""Liquidity layer: orderbook entropy H and H-derived features."""

import logging
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# WIKI_REQUIRED: H requires orderbook_collector running against OKX
# GET /api/v5/market/books?instId=BTC-USDT-SWAP&sz=20 every 60s.
# Each sample is stored in Redis list sel:h_samples:{symbol}:{bar_ts_iso}.
# At bar close the mean across ≥60 samples is persisted as H.
# Until the collector is deployed, H will always be None.


def compute_orderbook_entropy(levels: list[tuple[float, float]]) -> float:
    """
    levels: list of (price, size) tuples from one side of the orderbook.
    Returns Shannon entropy H = -Σ p_i * log(p_i) over the size distribution.
    """
    from oprim import orderbook_entropy

    sizes = np.array([size for _, size in levels if size > 0], dtype=float)
    return orderbook_entropy(sizes)


def compute_H_from_samples(samples: list[float]) -> tuple[Optional[float], int]:
    """Given per-minute H samples from a 1H bar, return (mean, count)."""
    valid = [s for s in samples if s is not None and np.isfinite(s)]
    if not valid:
        return None, 0
    return float(np.mean(valid)), len(valid)


def compute_H_change_rate_std(H_history: list[float], window: int = 12) -> Optional[float]:
    """12H rolling std of |ΔH/H| — measures how erratically entropy is changing."""
    # Need window+1 values to compute window differences
    if len(H_history) < window + 1:
        return None
    h = np.array(H_history[-(window + 1) :], dtype=float)
    # Avoid division by zero for zero-entropy bars
    with np.errstate(divide="ignore", invalid="ignore"):
        rates = np.where(h[:-1] != 0, np.abs(np.diff(h) / h[:-1]), np.nan)
    valid = rates[np.isfinite(rates)]
    if len(valid) < 2:
        return None
    return float(np.std(valid, ddof=1))
