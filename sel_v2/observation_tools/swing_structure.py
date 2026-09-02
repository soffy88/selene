"""ICT-2 swing-structure live observation tool (v2.2 batch).

Mechanized BOS/CHoCH over the shared 1.5x ATR zigzag — the same
sel_v2.offline.ict_lens.structure_series the offline study used, re-run over a
rolling window (one code path, no drift). Emits an event signal on the bar a
BOS/CHoCH crossing fires; the per-bar structure state rides in metadata.

Three-state discipline: a bar with missing high/low/close does not advance the
window and never signals. Observation-only (v2.1 §2.2).
"""

from __future__ import annotations

from collections import deque

from sel_v2.observation_tools.base import (
    BarFeatures,
    ObservationResult,
    ObservationTool,
)
from sel_v2.observation_tools.chan_tools import MIN_BARS, WINDOW_BARS, _warming

_STATE_VALUE = {"UP": 1.0, "DOWN": -1.0, "RANGE": 0.0}
_CHOCH_CONFIDENCE = 0.8  # character change — the stronger claim
_BOS_CONFIDENCE = 0.6  # trend continuation


class SwingStructureTool(ObservationTool):
    tool_id = "ICT2"

    def __init__(self) -> None:
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
            return _warming(self.tool_id, bar, 0.0)
        self._high.append(bar.high)
        self._low.append(bar.low)
        self._close.append(bar.close)
        if not self.is_ready():
            return _warming(self.tool_id, bar, 0.0)

        import numpy as np

        from sel_v2.offline.ict_lens import structure_series
        from sel_v2.offline.lens_common import compute_atr

        high = np.array(self._high)
        low = np.array(self._low)
        close = np.array(self._close)
        states, events = structure_series(close, compute_atr(high, low, close))
        struct = states[-1]
        last = len(close) - 1
        event = next((e for e in events if e.idx == last), None)
        if event is None:
            return ObservationResult(
                tool_id=self.tool_id,
                timestamp=bar.timestamp,
                signal=False,
                value=_STATE_VALUE[struct],
                threshold=0.0,
                label=struct,
                confidence=0.0,
                metadata={"structure": struct, "associated_state": bar.state},
            )
        return ObservationResult(
            tool_id=self.tool_id,
            timestamp=bar.timestamp,
            signal=True,
            value=_STATE_VALUE[struct],
            threshold=0.0,
            label=event.kind,
            confidence=(_CHOCH_CONFIDENCE if event.kind.startswith("CHOCH") else _BOS_CONFIDENCE),
            metadata={
                "structure": struct,
                "event": event.kind,
                "associated_state": bar.state,
            },
        )
