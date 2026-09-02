"""Fixtures for numerical regression tests."""

import numpy as np
import pandas as pd


def get_price_series(n: int = 500, seed: int = 42) -> pd.Series:
    """Synthetic BTC-like price series."""
    rng = np.random.default_rng(seed)
    log_rets = rng.normal(0.0002, 0.02, n)
    prices = 40000 * np.exp(np.cumsum(log_rets))
    idx = pd.date_range("2025-01-01", periods=n, freq="4h")
    return pd.Series(prices, index=idx, name="close")


def get_returns_series(n: int = 500, seed: int = 42) -> pd.Series:
    """Synthetic log returns."""
    rng = np.random.default_rng(seed)
    rets = rng.normal(0.0002, 0.02, n)
    idx = pd.date_range("2025-01-01", periods=n, freq="4h")
    return pd.Series(rets, index=idx, name="returns")


def get_returns_array(n: int = 500, seed: int = 42) -> np.ndarray:
    """Synthetic returns as numpy array."""
    rng = np.random.default_rng(seed)
    return rng.normal(0.0002, 0.02, n)


def get_ohlcv_df(n: int = 500, seed: int = 42) -> pd.DataFrame:
    """Synthetic OHLCV DataFrame."""
    rng = np.random.default_rng(seed)
    closes = 40000 * np.exp(np.cumsum(rng.normal(0.0002, 0.02, n)))
    highs = closes * (1 + rng.uniform(0, 0.01, n))
    lows = closes * (1 - rng.uniform(0, 0.01, n))
    opens = closes * (1 + rng.normal(0, 0.005, n))
    volume = rng.uniform(100, 10000, n)
    idx = pd.date_range("2025-01-01", periods=n, freq="4h")
    return pd.DataFrame({"open": opens, "high": highs, "low": lows, "close": closes, "volume": volume}, index=idx)
