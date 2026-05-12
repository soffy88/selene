"""
CUSUM-Short accumulator  (sel-language-v2.0 §11.3 / Strategy 2)

Page CUSUM for 1-second tick return z-scores, used as the primary entry
trigger for Strategy 2 (intraday short-term).

Parameters (all placeholders per §11.4 — calibrate after paper Month 1):
  drift_k     0.5σ  (classic Page CUSUM recommendation)
  threshold   7-day rolling 95th percentile of CUSUM peak values

Accumulator:
  C+_t = max(0, C+_{t-1} + z_t - k)   (upward pressure)
  C-_t = max(0, C-_{t-1} - z_t - k)   (downward pressure)

Trigger:
  C+_t > h_t  → LONG candidate
  C-_t > h_t  → SHORT candidate

Reset after trigger (each accumulator resets independently).
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Deque, Literal, Optional, Tuple

import numpy as np

# Placeholder calibration per v2.0 §11.4
DEFAULT_DRIFT_K: float = 0.5
DEFAULT_THRESHOLD_QUANTILE: float = 0.95
DEFAULT_THRESHOLD_WINDOW_BARS: int = 7 * 24 * 3600  # 7 days in seconds (1-sec bars)


@dataclass
class CUSUMTrigger:
    triggered: bool
    direction: Optional[Literal["LONG", "SHORT"]]
    cusum_positive: float    # current C+
    cusum_negative: float    # current C-
    threshold: float         # current h_t
    intensity_coeff: float   # C/h ratio (for position sizing)


class CUSUMShort:
    """
    Strategy 2 CUSUM-Short accumulator.

    Accepts z_t = (r_t - μ_t) / σ_t where μ_t and σ_t are the rolling
    10-minute mean and std of 1-second returns (computed externally).

    Maintains:
      - C+, C-: Page CUSUM accumulators
      - Rolling peak history for threshold computation
    """

    def __init__(
        self,
        drift_k: float = DEFAULT_DRIFT_K,
        threshold_quantile: float = DEFAULT_THRESHOLD_QUANTILE,
        threshold_window_sec: int = DEFAULT_THRESHOLD_WINDOW_BARS,
    ) -> None:
        self.drift_k = drift_k
        self.threshold_quantile = threshold_quantile
        self.threshold_window_sec = threshold_window_sec

        self._c_pos: float = 0.0
        self._c_neg: float = 0.0
        self._peak_pos: float = 0.0    # peak since last positive reset
        self._peak_neg: float = 0.0    # peak since last negative reset

        # Rolling peak history: (timestamp_sec, peak_value)
        self._pos_peaks: Deque[Tuple[float, float]] = deque()
        self._neg_peaks: Deque[Tuple[float, float]] = deque()

    # ── Update ────────────────────────────────────────────────────────────

    def update(self, z_t: float, t: Optional[float] = None) -> CUSUMTrigger:
        """
        Update CUSUM with standardised return z_t at time t.
        Returns trigger state (check .triggered before acting).
        """
        self._c_pos = max(0.0, self._c_pos + z_t - self.drift_k)
        self._c_neg = max(0.0, self._c_neg - z_t - self.drift_k)

        if self._c_pos > self._peak_pos:
            self._peak_pos = self._c_pos
        if self._c_neg > self._peak_neg:
            self._peak_neg = self._c_neg

        h = self._current_threshold(t)
        triggered_pos = self._c_pos > h
        triggered_neg = self._c_neg > h

        # Both triggered simultaneously — take dominant, don't enter
        if triggered_pos and triggered_neg:
            # Resolve by which is larger; if tied, abort
            if self._c_pos > self._c_neg:
                triggered_neg = False
            elif self._c_neg > self._c_pos:
                triggered_pos = False
            else:
                return CUSUMTrigger(
                    triggered=False,
                    direction=None,
                    cusum_positive=self._c_pos,
                    cusum_negative=self._c_neg,
                    threshold=h,
                    intensity_coeff=0.0,
                )

        if triggered_pos:
            coeff = self._c_pos / h if h > 0 else 1.0
            self._record_peak(t, self._peak_pos, positive=True)
            return CUSUMTrigger(
                triggered=True,
                direction="LONG",
                cusum_positive=self._c_pos,
                cusum_negative=self._c_neg,
                threshold=h,
                intensity_coeff=min(coeff, 3.0),  # cap per §14.4
            )

        if triggered_neg:
            coeff = self._c_neg / h if h > 0 else 1.0
            self._record_peak(t, self._peak_neg, positive=False)
            return CUSUMTrigger(
                triggered=True,
                direction="SHORT",
                cusum_positive=self._c_pos,
                cusum_negative=self._c_neg,
                threshold=h,
                intensity_coeff=min(coeff, 3.0),
            )

        return CUSUMTrigger(
            triggered=False,
            direction=None,
            cusum_positive=self._c_pos,
            cusum_negative=self._c_neg,
            threshold=h,
            intensity_coeff=0.0,
        )

    def reset_positive(self) -> None:
        """Reset C+ after LONG entry (standard Page CUSUM post-trigger reset)."""
        self._c_pos = 0.0
        self._peak_pos = 0.0

    def reset_negative(self) -> None:
        """Reset C- after SHORT entry."""
        self._c_neg = 0.0
        self._peak_neg = 0.0

    def reset_all(self) -> None:
        self.reset_positive()
        self.reset_negative()

    # ── Threshold ─────────────────────────────────────────────────────────

    def _current_threshold(self, t: Optional[float]) -> float:
        """7-day rolling 95th pct of peak values; fallback=2.0 when cold."""
        self._evict(t)
        peaks = [v for _, v in self._pos_peaks] + [v for _, v in self._neg_peaks]
        if len(peaks) < 20:
            return 2.0  # cold-start default: require 2 sigma equivalent
        from oprim import percentile_value
        return float(percentile_value(np.array(peaks), self.threshold_quantile))

    def _record_peak(self, t: Optional[float], peak: float, positive: bool) -> None:
        if t is None:
            return
        target = self._pos_peaks if positive else self._neg_peaks
        target.append((t, peak))

    def _evict(self, t: Optional[float]) -> None:
        if t is None:
            return
        cutoff = t - self.threshold_window_sec
        for buf in (self._pos_peaks, self._neg_peaks):
            while buf and buf[0][0] < cutoff:
                buf.popleft()

    @property
    def state(self) -> dict:
        return {
            "c_pos": self._c_pos,
            "c_neg": self._c_neg,
            "peak_pos": self._peak_pos,
            "peak_neg": self._peak_neg,
        }
