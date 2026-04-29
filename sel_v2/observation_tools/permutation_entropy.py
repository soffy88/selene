"""
I1 — Permutation Entropy anomaly detector  (v2.1 §10.1)

Rolling permutation entropy of 4H log-returns using pure numpy.

STUB: antropy unavailable → implemented from scratch using ordinal pattern
      counting (Bandt & Pompe 2002).

Configuration (v2.1 §10.1):
  order d = 3   (3-bar ordinal patterns → 3! = 6 symbols)
  window  = 20 bars
  signal fires when H_perm < LOW_ENTROPY_THRESHOLD (structured / directional)
  or > HIGH_ENTROPY_THRESHOLD (near-random / regime breakdown)

Output value: H_perm normalised to [0, 1].
"""
from __future__ import annotations

from collections import deque
from itertools import permutations
from typing import ClassVar

import numpy as np

from sel_v2.observation_tools.base import BarFeatures, ObservationResult, ObservationTool

ORDER = 3
WINDOW_BARS = 20
MIN_BARS = ORDER + 5
LOW_ENTROPY_THRESHOLD = 0.35    # structured signal → possible directional regime
HIGH_ENTROPY_THRESHOLD = 0.90   # near-random → noise / breakdown

# Precompute ordinal-pattern rank map
_PERM_RANK: dict[tuple[int, ...], int] = {
    p: i for i, p in enumerate(permutations(range(ORDER)))
}
_N_PATTERNS = len(_PERM_RANK)


def _permutation_entropy(x: np.ndarray, order: int = ORDER) -> float:
    """
    Compute normalised permutation entropy in [0, 1] for array x.
    Returns 1.0 (maximum disorder) if degenerate.
    """
    n = len(x) - order + 1
    if n <= 0:
        return 1.0
    counts = np.zeros(_N_PATTERNS, dtype=np.float64)
    for i in range(n):
        w = x[i: i + order]
        rank = tuple(int(r) for r in np.argsort(np.argsort(w)))
        counts[_PERM_RANK[rank]] += 1
    probs = counts[counts > 0] / n
    H = -float(np.sum(probs * np.log2(probs)))
    H_max = np.log2(_N_PATTERNS)
    return H / H_max if H_max > 0 else 0.0


class PermutationEntropy(ObservationTool):
    """
    I1 — rolling permutation entropy detector  (v2.1 §10.1).

    Fires when H_perm falls below LOW_ENTROPY_THRESHOLD (structured directional
    signal) or rises above HIGH_ENTROPY_THRESHOLD (random/noise regime).
    """
    tool_id = "I1"

    def __init__(
        self,
        order: int = ORDER,
        window: int = WINDOW_BARS,
        low_threshold: float = LOW_ENTROPY_THRESHOLD,
        high_threshold: float = HIGH_ENTROPY_THRESHOLD,
    ) -> None:
        self._order = order
        self._window = window
        self._low = low_threshold
        self._high = high_threshold
        self._buffer: deque[float] = deque(maxlen=window)

    def is_ready(self) -> bool:
        return len(self._buffer) >= MIN_BARS

    def update(self, bar: BarFeatures) -> ObservationResult:
        self._buffer.append(bar.log_return)

        if not self.is_ready():
            return ObservationResult(
                tool_id=self.tool_id,
                timestamp=bar.timestamp,
                signal=False,
                value=0.5,
                threshold=self._low,
                label="WARMING",
                confidence=0.0,
            )

        x = np.array(self._buffer)
        h = _permutation_entropy(x, order=self._order)

        low_signal = h < self._low
        high_signal = h > self._high
        signal = low_signal or high_signal

        if low_signal:
            label = "STRUCTURED"
        elif high_signal:
            label = "RANDOM"
        else:
            label = "NORMAL"

        # Confidence: distance from nearest threshold
        if low_signal:
            confidence = min((self._low - h) / max(self._low, 1e-8), 1.0)
        elif high_signal:
            confidence = min((h - self._high) / max(1.0 - self._high, 1e-8), 1.0)
        else:
            confidence = 0.0

        return ObservationResult(
            tool_id=self.tool_id,
            timestamp=bar.timestamp,
            signal=signal,
            value=h,
            threshold=self._low,
            label=label,
            confidence=confidence,
            metadata={
                "high_threshold": self._high,
                "order": self._order,
                "n_patterns": _N_PATTERNS,
            },
        )

    def reset(self) -> None:
        self._buffer.clear()
