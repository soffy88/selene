"""
TDA2 — Online TDA clustering  (v2.1 §12.2)

Online variant of the TDA1 offline calibration.  Reuses the Takens embedding
and persistence landscape routines from sel_v2.offline.tda_calibration.

Rolling window of WINDOW_BARS 4H log-returns → Takens embedding → Vietoris-
Rips via ripser → H1 persistence landscape L^1 norm.  Fires when norm exceeds
the rolling 90th-percentile threshold.

STUB: threshold is seeded from hardcoded default (0.05) until a 90-day
      calibration history is accumulated.  Replace via offline recalibration.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime

import numpy as np

from sel_v2.observation_tools.base import BarFeatures, ObservationResult, ObservationTool
from sel_v2.offline.tda_calibration import takens_embed

try:
    from ripser import ripser as _ripser
    _HAS_RIPSER = True
except ImportError:
    _HAS_RIPSER = False

WINDOW_BARS = 40        # Takens embedding window
MIN_BARS = 20           # minimum for meaningful embedding
DEFAULT_THRESHOLD = 0.05
QUANTILE_WINDOW = 360   # bars for rolling 90th-pct (~60 days of 4H)
EMBED_DIM = 4
EMBED_TAU = 1


def _persistence_landscape_l1(x: np.ndarray, d: int = EMBED_DIM, tau: int = EMBED_TAU) -> float:
    """
    Compute H1 persistence landscape L^1 norm for a 1-D time series.
    Returns 0.0 if ripser is unavailable or the embedding is degenerate.
    """
    if not _HAS_RIPSER:
        return 0.0
    try:
        pts = takens_embed(x, d=d, tau=tau)
        if len(pts) < 4:
            return 0.0
        result = _ripser(pts, maxdim=1)
        dgm1 = result["dgms"][1]
        if len(dgm1) == 0:
            return 0.0
        # Remove infinite bars
        finite_mask = np.isfinite(dgm1[:, 1])
        if not finite_mask.any():
            return 0.0
        dgm1 = dgm1[finite_mask]
        # L^1 norm = sum of bar lengths
        return float(np.sum(dgm1[:, 1] - dgm1[:, 0]))
    except Exception:
        return 0.0


class TDAClustering(ObservationTool):
    """
    TDA2 — rolling persistence landscape anomaly detector  (v2.1 §12.2).
    """
    tool_id = "TDA2"

    def __init__(
        self,
        window: int = WINDOW_BARS,
        default_threshold: float = DEFAULT_THRESHOLD,
    ) -> None:
        self._window = window
        self._buffer: deque[float] = deque(maxlen=window)
        self._l1_history: deque[float] = deque(maxlen=QUANTILE_WINDOW)
        self._default_threshold = default_threshold

    def is_ready(self) -> bool:
        return len(self._buffer) >= MIN_BARS

    def _threshold(self) -> float:
        if len(self._l1_history) < 30:
            return self._default_threshold
        return float(np.quantile(self._l1_history, 0.90))

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
        l1 = _persistence_landscape_l1(x)
        self._l1_history.append(l1)
        thresh = self._threshold()
        anomaly = l1 > thresh

        return ObservationResult(
            tool_id=self.tool_id,
            timestamp=bar.timestamp,
            signal=anomaly,
            value=l1,
            threshold=thresh,
            label="TDA_ANOMALY" if anomaly else "NORMAL",
            confidence=min(l1 / max(thresh, 1e-8), 1.0) if anomaly else 0.0,
            metadata={
                "ripser_available": _HAS_RIPSER,
                "l1_norm": l1,
                "quantile_samples": len(self._l1_history),
            },
        )

    def reset(self) -> None:
        self._buffer.clear()
        self._l1_history.clear()
