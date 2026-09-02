"""
B2 — HMM Boundary Arbiter  (v2.1 §8.2)

Detects regime boundary crossings using B1 posterior probability shifts.
A boundary event fires when the HIGH_VOL posterior changes by more than
SHIFT_THRESHOLD in a single 4H step.

Output label: "BOUNDARY_TO_HIGH" | "BOUNDARY_TO_LOW" | "STABLE"
"""

from __future__ import annotations

from typing import Optional

from sel_v2.observation_tools.base import BarFeatures, ObservationResult, ObservationTool
from sel_v2.observation_tools.bayesian_hmm import BayesianHMM

SHIFT_THRESHOLD = 0.30  # v2.1 §8.2: posterior shift > 0.30 triggers boundary


class HMMBoundaryArbiter(ObservationTool):
    """
    B2 — regime boundary detector built on B1 posteriors  (v2.1 §8.2).

    Fires when B1's HIGH_VOL posterior shifts by more than SHIFT_THRESHOLD
    in a single bar.  Transition direction is encoded in the label.
    """

    tool_id = "B2"

    def __init__(self, shift_threshold: float = SHIFT_THRESHOLD) -> None:
        self._b1 = BayesianHMM()
        self._shift_threshold = shift_threshold
        self._prev_proba: Optional[float] = None

    def is_ready(self) -> bool:
        return self._b1.is_ready() and self._prev_proba is not None

    def update(self, bar: BarFeatures) -> ObservationResult:
        b1_result = self._b1.update(bar)
        current_proba = self._b1.last_high_vol_proba

        if not self._b1.is_ready():
            self._prev_proba = current_proba
            return ObservationResult(
                tool_id=self.tool_id,
                timestamp=bar.timestamp,
                signal=False,
                value=0.0,
                threshold=self._shift_threshold,
                label="WARMING",
                confidence=0.0,
            )

        prev = self._prev_proba if self._prev_proba is not None else current_proba
        shift = current_proba - prev
        self._prev_proba = current_proba

        boundary = abs(shift) >= self._shift_threshold
        if boundary:
            if shift > 0:
                label = "BOUNDARY_TO_HIGH"
            else:
                label = "BOUNDARY_TO_LOW"
        else:
            label = "STABLE"

        return ObservationResult(
            tool_id=self.tool_id,
            timestamp=bar.timestamp,
            signal=boundary,
            value=abs(shift),
            threshold=self._shift_threshold,
            label=label,
            confidence=min(abs(shift) / max(self._shift_threshold, 1e-8), 1.0),
            metadata={
                "prev_proba": float(prev),
                "current_proba": float(current_proba),
                "shift": float(shift),
                "b1_label": b1_result.label,
            },
        )

    def reset(self) -> None:
        self._b1.reset()
        self._prev_proba = None
