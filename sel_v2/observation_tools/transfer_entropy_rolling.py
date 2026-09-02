"""
T2 — Rolling Transfer Entropy  (v2.1 §6.2)

Reuses the Symbolic Transfer Entropy (STE) implementation from
sel_v2.offline.transfer_entropy.  Computes rolling STE from funding_rate to
BTC log-returns over a 30-bar window.

STUB: data-starved until ≥ 90-day funding history has accumulated.
      funding_rate may be None → tool silently returns HOLD with signal=False.

Signal fires when STE(funding→returns) exceeds the rolling 90th-percentile
threshold, indicating funding rate is causally predictive of price.
"""

from __future__ import annotations

from collections import deque

import numpy as np

from sel_v2.observation_tools.base import BarFeatures, ObservationResult, ObservationTool
from sel_v2.offline.transfer_entropy import symbolic_te

WINDOW_BARS = 30
MIN_BARS = 15  # STE needs at least this many observations
QUANTILE_WINDOW = 360  # bars for rolling 90th-pct
DEFAULT_THRESHOLD = 0.10  # bits; seeded until history accumulates


class TransferEntropyRolling(ObservationTool):
    """
    T2 — rolling STE(funding → returns)  (v2.1 §6.2).

    STUB: signals are data-quality-gated.  Returns False when funding_rate
    history is insufficient.
    """

    tool_id = "T2"

    def __init__(
        self,
        window: int = WINDOW_BARS,
        default_threshold: float = DEFAULT_THRESHOLD,
    ) -> None:
        self._window = window
        self._returns: deque[float] = deque(maxlen=window)
        self._funding: deque[float] = deque(maxlen=window)
        self._ste_history: deque[float] = deque(maxlen=QUANTILE_WINDOW)
        self._default_threshold = default_threshold
        self._funding_bars_seen = 0

    def is_ready(self) -> bool:
        return len(self._returns) >= MIN_BARS and len(self._funding) >= MIN_BARS

    def _threshold(self) -> float:
        if len(self._ste_history) < 30:
            return self._default_threshold
        return float(np.quantile(self._ste_history, 0.90))

    def update(self, bar: BarFeatures) -> ObservationResult:
        self._returns.append(bar.log_return)

        if bar.funding_rate is None:
            # STUB: funding data not yet available
            return ObservationResult(
                tool_id=self.tool_id,
                timestamp=bar.timestamp,
                signal=False,
                value=0.0,
                threshold=self._default_threshold,
                label="NO_DATA",
                confidence=0.0,
                metadata={"stub": "funding_rate_missing"},
            )

        self._funding.append(bar.funding_rate)
        self._funding_bars_seen += 1

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

        src = np.array(self._funding)
        tgt = np.array(self._returns)
        try:
            ste = float(symbolic_te(source=src, target=tgt))
        except Exception:
            ste = 0.0

        self._ste_history.append(ste)
        thresh = self._threshold()
        signal = ste > thresh

        return ObservationResult(
            tool_id=self.tool_id,
            timestamp=bar.timestamp,
            signal=signal,
            value=ste,
            threshold=thresh,
            label="FUNDING_CAUSAL" if signal else "NORMAL",
            confidence=min(ste / max(thresh, 1e-8), 1.0) if signal else 0.0,
            metadata={
                "funding_bars_seen": self._funding_bars_seen,
                "ste_samples": len(self._ste_history),
            },
        )

    def reset(self) -> None:
        self._returns.clear()
        self._funding.clear()
        self._ste_history.clear()
        self._funding_bars_seen = 0
