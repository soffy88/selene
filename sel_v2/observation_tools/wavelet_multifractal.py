"""
W2 — Wavelet Multifractal detector  (v2.1 §9.2)

Rolling DWT db4 decomposition of 4H log-returns, 6 levels.
Detects anomalous multifractal signatures by tracking the ratio of
high-frequency (levels 1-2) to low-frequency (levels 5-6) wavelet energy.

A Cascade regime typically shows concentrated high-freq energy (spiky)
relative to low-freq trend energy.  Signal fires when energy ratio exceeds
rolling 90th-percentile threshold.

Reuses pywt (available) from sel_v2/offline/wavelet.py pattern.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime

import numpy as np
import pywt

from sel_v2.observation_tools.base import BarFeatures, ObservationResult, ObservationTool

WAVELET = "db4"
DWT_LEVELS = 6
WINDOW_BARS = 64        # must be ≥ 2^DWT_LEVELS for complete decomposition
MIN_BARS = 32           # minimum to produce at least a partial decomposition
QUANTILE_WINDOW = 360   # bars for rolling 90th-pct
DEFAULT_THRESHOLD = 2.0  # high/low energy ratio


def _energy_ratio(x: np.ndarray, wavelet: str = WAVELET, level: int = DWT_LEVELS) -> float:
    """
    Compute (E_level1 + E_level2) / (E_level5 + E_level6) as a
    multifractal proxy.  Returns 0.0 if decomposition fails.
    """
    try:
        coeffs = pywt.wavedec(x, wavelet, level=min(level, pywt.dwt_max_level(len(x), wavelet)))
        # coeffs[0] = approx, coeffs[1..] = details from finest to coarsest
        # detail[0] = finest (level 1), detail[-1] = coarsest
        details = coeffs[1:]
        energies = [float(np.sum(c ** 2)) for c in details]
        if len(energies) < 2:
            return 0.0
        high_freq = sum(energies[:2])
        low_freq = sum(energies[-2:]) if len(energies) >= 4 else sum(energies[-1:])
        if low_freq < 1e-12:
            return 0.0
        return high_freq / low_freq
    except Exception:
        return 0.0


class WaveletMultifractal(ObservationTool):
    """
    W2 — rolling wavelet multifractal anomaly detector  (v2.1 §9.2).
    """
    tool_id = "W2"

    def __init__(
        self,
        window: int = WINDOW_BARS,
        default_threshold: float = DEFAULT_THRESHOLD,
    ) -> None:
        self._window = window
        self._buffer: deque[float] = deque(maxlen=window)
        self._ratio_history: deque[float] = deque(maxlen=QUANTILE_WINDOW)
        self._default_threshold = default_threshold

    def is_ready(self) -> bool:
        return len(self._buffer) >= MIN_BARS

    def _threshold(self) -> float:
        if len(self._ratio_history) < 30:
            return self._default_threshold
        return float(np.quantile(self._ratio_history, 0.90))

    def update(self, bar: BarFeatures) -> ObservationResult:
        self._buffer.append(bar.log_return)

        if not self.is_ready():
            return ObservationResult(
                tool_id=self.tool_id,
                timestamp=bar.timestamp,
                signal=False,
                value=0.0,
                threshold=self._default_threshold,
                label="WARMING",
                confidence=0.0,
            )

        x = np.array(self._buffer)
        ratio = _energy_ratio(x)
        self._ratio_history.append(ratio)
        thresh = self._threshold()
        anomaly = ratio > thresh

        return ObservationResult(
            tool_id=self.tool_id,
            timestamp=bar.timestamp,
            signal=anomaly,
            value=ratio,
            threshold=thresh,
            label="MULTIFRACTAL_ANOMALY" if anomaly else "NORMAL",
            confidence=min(ratio / max(thresh, 1e-8), 1.0) if anomaly else 0.0,
            metadata={
                "wavelet": WAVELET,
                "dwt_levels": DWT_LEVELS,
                "ratio_samples": len(self._ratio_history),
            },
        )

    def reset(self) -> None:
        self._buffer.clear()
        self._ratio_history.clear()
