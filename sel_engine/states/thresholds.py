"""Rolling quantile calculator for strictly causal state recognition."""
import numpy as np
from typing import Optional
from collections import deque


class RollingQuantileCalculator:
    """
    Maintains a fixed-size rolling window of feature values and computes
    quantile rank of the current value against that window.
    Strictly causal: window contains only past values (not current bar).
    """
    WINDOW = 720  # 30 days × 24H bars
    MIN_VALUES = 10  # minimum non-None values to compute a quantile

    def __init__(self):
        self._windows: dict[str, deque] = {}  # keyed by feature name

    def add(self, feature_name: str, value: Optional[float]) -> None:
        """Add value to the window AFTER computing the current bar's quantile."""
        if feature_name not in self._windows:
            self._windows[feature_name] = deque(maxlen=self.WINDOW)
        self._windows[feature_name].append(value)

    def quantile_rank(self, feature_name: str, current_value: Optional[float]) -> Optional[float]:
        """
        Returns quantile rank of current_value in [0, 1] against the CURRENT window
        (which contains only past values — add() must be called AFTER this).
        Returns None if insufficient history or value is None.
        """
        if current_value is None:
            return None
        window = self._windows.get(feature_name)
        if not window:
            return None
        values = [v for v in window if v is not None]
        if len(values) < self.MIN_VALUES:
            return None
        arr = np.array(values, dtype=float)
        # fraction of past values strictly below current_value
        rank = float(np.sum(arr < current_value)) / len(arr)
        return rank

    def is_cold_start(self, feature_name: str) -> bool:
        """True if window has fewer than WINDOW entries (still warming up)."""
        window = self._windows.get(feature_name)
        return window is None or len(window) < self.WINDOW

    def bar_count(self) -> int:
        """Number of bars processed (based on close price window)."""
        window = self._windows.get("close")
        return len(window) if window else 0
