"""Chan (缠论) lens live observation tools — CHAN-2 / CHAN-3 (v2.2 batch).

CHAN-1 (chan_retest) was REJECTED by the offline gate (H-CHAN1 fail on both
consolidation configs, analysis/lens_verdict_v1.md) and is intentionally absent.

Both tools re-run the SAME pure functions the offline study used
(sel_v2.offline.chan_lens / lens_common) over an internal rolling window, so
live signals and offline hypothesis tests cannot drift apart. Frozen parameters
come from analysis/lens_verdict_v1.md (single source of truth).

Three-state discipline: a bar with missing high/low/close (or, for CHAN-2, a
missing sel state) does NOT advance detection state and never signals.
Observation-only — never called from any strategy path (v2.1 §2.2).
"""

from __future__ import annotations

from collections import deque

from sel_v2.observation_tools.base import (
    BarFeatures,
    ObservationResult,
    ObservationTool,
)

WINDOW_BARS = 560  # rolling window (>= the 540-bar replay the paper engine feeds)
MIN_BARS = 60  # ATR + >=3 confirmed swings before anything is emitted

# Frozen live parameters — analysis/lens_verdict_v1.md (2026-07-11 offline study)
CHAN_PIVOT_OVERLAP_THRESHOLD = 2.328  # p70 of overlap_ratio over the 2yr sample
DIVERGENCE_RATIO_THRESHOLD = 0.7  # candidate-pool definition (CHAN-2)


def _warming(tool_id: str, bar: BarFeatures, threshold: float) -> ObservationResult:
    return ObservationResult(
        tool_id=tool_id,
        timestamp=bar.timestamp,
        signal=False,
        value=0.0,
        threshold=threshold,
        label="WARMING",
        confidence=0.0,
    )


class ChanPivot(ObservationTool):
    """CHAN-3 中枢重叠度 — fires when the last-3-confirmed-swing price overlap
    (in ATR units) exceeds the frozen offline p70."""

    tool_id = "CHAN3"

    def __init__(self, threshold: float = CHAN_PIVOT_OVERLAP_THRESHOLD) -> None:
        self._threshold = threshold
        self._high: deque[float] = deque(maxlen=WINDOW_BARS)
        self._low: deque[float] = deque(maxlen=WINDOW_BARS)
        self._close: deque[float] = deque(maxlen=WINDOW_BARS)

    def is_ready(self) -> bool:
        return len(self._close) >= MIN_BARS

    def reset(self) -> None:
        self._high.clear()
        self._low.clear()
        self._close.clear()

    def update(self, bar: BarFeatures) -> ObservationResult:
        if bar.high is None or bar.low is None or bar.close is None:
            return _warming(self.tool_id, bar, self._threshold)  # no state advance
        self._high.append(bar.high)
        self._low.append(bar.low)
        self._close.append(bar.close)
        if not self.is_ready():
            return _warming(self.tool_id, bar, self._threshold)

        import numpy as np

        from sel_v2.offline.chan_lens import pivot_overlap_series
        from sel_v2.offline.lens_common import compute_atr

        high = np.array(self._high)
        low = np.array(self._low)
        close = np.array(self._close)
        atr = compute_atr(high, low, close)
        ratio = float(pivot_overlap_series(close, atr)[-1])
        if not np.isfinite(ratio):
            return _warming(self.tool_id, bar, self._threshold)
        signal = ratio > self._threshold
        return ObservationResult(
            tool_id=self.tool_id,
            timestamp=bar.timestamp,
            signal=signal,
            value=ratio,
            threshold=self._threshold,
            label="PIVOT_OVERLAP" if signal else "NORMAL",
            confidence=(min((ratio - self._threshold) / max(self._threshold, 1e-8), 1.0) if signal else 0.0),
            metadata={"associated_state": bar.state},
        )


class ChanDivergence(ObservationTool):
    """CHAN-2 背驰 — fires on a divergence-candidate bar inside an annotated
    Surging leg (price beyond the prior same-direction leg's extreme while
    per-bar momentum < 0.7x the prior leg's). Needs `bar.state`; a None state
    freezes leg bookkeeping entirely (three-state discipline)."""

    tool_id = "CHAN2"

    def __init__(self, ratio_threshold: float = DIVERGENCE_RATIO_THRESHOLD) -> None:
        self._ratio = ratio_threshold
        self._close: deque[float] = deque(maxlen=WINDOW_BARS)
        self._state: deque[str] = deque(maxlen=WINDOW_BARS)

    def is_ready(self) -> bool:
        return len(self._close) >= MIN_BARS

    def reset(self) -> None:
        self._close.clear()
        self._state.clear()

    def update(self, bar: BarFeatures) -> ObservationResult:
        if bar.close is None or bar.state is None:
            return _warming(self.tool_id, bar, self._ratio)  # freeze bookkeeping
        self._close.append(bar.close)
        self._state.append(bar.state)
        if not self.is_ready():
            return _warming(self.tool_id, bar, self._ratio)

        import numpy as np

        from sel_v2.offline.chan_lens import detect_divergences
        from sel_v2.offline.lens_common import surging_legs

        close = np.array(self._close)
        states = list(self._state)
        legs = surging_legs(states, [None] * len(states), close)
        cands, _testable = detect_divergences(legs, close)
        last = len(close) - 1
        hit = next((c for c in cands if c.bar_idx == last), None)
        if hit is None:
            return ObservationResult(
                tool_id=self.tool_id,
                timestamp=bar.timestamp,
                signal=False,
                value=0.0,
                threshold=self._ratio,
                label="NORMAL",
                confidence=0.0,
                metadata={"associated_state": bar.state},
            )
        ratio = hit.momentum / hit.prior_momentum if hit.prior_momentum > 0 else 0.0
        return ObservationResult(
            tool_id=self.tool_id,
            timestamp=bar.timestamp,
            signal=True,
            value=ratio,
            threshold=self._ratio,
            label="DIVERGENCE",
            confidence=min((self._ratio - ratio) / max(self._ratio, 1e-8), 1.0),
            metadata={
                "associated_state": bar.state,
                "momentum": hit.momentum,
                "prior_momentum": hit.prior_momentum,
            },
        )
