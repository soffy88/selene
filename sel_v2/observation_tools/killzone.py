"""ICT-3 Killzone session-adjusted anomaly tool (2026-07-11 ICT batch).

The killzone hypothesis PASSED the preregistered offline gate
(analysis/ict_advanced_v1.md: KW p=5.8e-26 on |ret|, p=6e-145 on volume,
date-half robust, top slot 12:00 UTC) — 4H bars have strong, stable
time-of-day seasonality. The actionable embodiment is NOT the clock itself
(that carries no information) but SESSION-ADJUSTED anomaly detection: a move/
volume that is abnormal FOR ITS OWN SLOT. An unconditional threshold over-fires
in the busy 12:00 slot and under-fires in the quiet 04:00 slot (1.7x |ret| /
2.5x volume apart).

Fires when the bar's |log return| AND volume both exceed their own slot's
rolling p90 (joint condition — engineering choice, not a fitted parameter;
documented as such). Rolling per-slot history = last 180 same-slot bars
(~6 months), ready after 30.

Observation-only (v2.1 §2.2): never feeds any strategy decision path.
"""

from __future__ import annotations

from collections import deque

from sel_v2.observation_tools.base import (
    BarFeatures,
    ObservationResult,
    ObservationTool,
)

SLOT_HISTORY = 180  # same-slot bars kept (~6 months)
MIN_SLOT_SAMPLES = 30
PCTILE = 0.90  # joint |ret| AND volume threshold — engineering choice


def _pctile_rank(hist, x: float) -> float:
    if not hist:
        return 0.0
    return sum(1 for v in hist if v <= x) / len(hist)


class KillzoneAnomaly(ObservationTool):
    tool_id = "ICT3"

    def __init__(self) -> None:
        self._ret: dict[int, deque] = {}
        self._vol: dict[int, deque] = {}

    def is_ready(self) -> bool:
        return any(len(d) >= MIN_SLOT_SAMPLES for d in self._ret.values())

    def reset(self) -> None:
        self._ret.clear()
        self._vol.clear()

    def update(self, bar: BarFeatures) -> ObservationResult:
        slot = bar.timestamp.hour
        absret = abs(bar.log_return)
        rets = self._ret.setdefault(slot, deque(maxlen=SLOT_HISTORY))
        vols = self._vol.setdefault(slot, deque(maxlen=SLOT_HISTORY))
        # rank against history EXCLUDING the current bar, then append
        ret_rank = _pctile_rank(rets, absret)
        vol_rank = _pctile_rank(vols, bar.volume)
        enough = len(rets) >= MIN_SLOT_SAMPLES
        rets.append(absret)
        vols.append(bar.volume)
        if not enough:
            return ObservationResult(
                tool_id=self.tool_id,
                timestamp=bar.timestamp,
                signal=False,
                value=0.0,
                threshold=PCTILE,
                label="WARMING",
                confidence=0.0,
            )
        signal = ret_rank > PCTILE and vol_rank > PCTILE
        return ObservationResult(
            tool_id=self.tool_id,
            timestamp=bar.timestamp,
            signal=signal,
            value=ret_rank,
            threshold=PCTILE,
            label=f"KZ{slot:02d}_ANOMALY" if signal else f"KZ{slot:02d}",
            confidence=min(1.0, (min(ret_rank, vol_rank) - PCTILE) / (1 - PCTILE))
            if signal
            else 0.0,
            metadata={
                "slot_utc": slot,
                "ret_slot_pctile": round(ret_rank, 3),
                "vol_slot_pctile": round(vol_rank, 3),
                "associated_state": bar.state,
            },
        )
