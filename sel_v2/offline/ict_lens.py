"""ICT lens — pure functions for the ICT-2 candidate + VPIN pilot stats (v2.2 pool).

ICT-2 swing structure: mechanized BOS/CHoCH over the same 1.5x ATR zigzag as
CHAN-3 (shared infra per the pool spec). The structure STATE updates only when a
swing confirms; the per-bar BOS/CHoCH events are the timely layer. Everything is
keyed on zigzag confirmation instants (lens_common.Swing.confirm_idx) — no
look-ahead.

ICT-1 VPIN: the streaming calculator lives in sel_v2/observation_tools/vpin.py
(shared by the live monitor); this module only holds the offline pilot statistics.

Offline-only home; the live SwingStructureTool imports structure logic from
lens_common (one code path). Touches nothing under strategies/**, states/**, or
the epoch.
"""

from __future__ import annotations

import dataclasses

import numpy as np

from sel_v2.offline.lens_common import SwingStructure, atr_zigzag_swings


@dataclasses.dataclass
class StructureEvent:
    idx: int  # bar at which the crossing fired (causal)
    kind: str  # BOS_UP / BOS_DOWN / CHOCH_UP / CHOCH_DOWN


def structure_series(
    close: np.ndarray, atr: np.ndarray
) -> tuple[list[str], list[StructureEvent]]:
    """Drive a SwingStructure machine over the full series. Swings are fed in at
    their confirm_idx (BEFORE the same bar's break check — both are knowable at
    that bar's close). Returns (per-bar structure state, event list)."""
    swings = atr_zigzag_swings(close, atr)
    machine = SwingStructure()
    states: list[str] = []
    events: list[StructureEvent] = []
    k = 0
    for i in range(len(close)):
        while k < len(swings) and swings[k].confirm_idx == i:
            machine.on_swing_confirmed(swings[k], close)
            k += 1
        ev = machine.on_bar(float(close[i]))
        if ev is not None:
            events.append(StructureEvent(idx=i, kind=ev))
        states.append(machine.state)
    return states, events


# ── ICT-1 VPIN pilot statistics ──────────────────────────────────────────────


def vpin_pilot_stats(vpin_series: list[tuple], bucket_minutes: list[float]) -> dict:
    """Descriptive pilot stats over completed VPIN buckets.

    vpin_series: [(bucket_close_ts, side_vpin, bvc_vpin), ...] with None entries
    while the 50-bucket window is filling. Returns distribution / duration /
    autocorrelation / side-vs-BVC agreement numbers for the ICT report."""
    side = np.array([v for _ts, v, _b in vpin_series if v is not None], dtype=float)
    # side-vs-BVC agreement over PAIRED points only (bvc_vpin warms up one bucket
    # later than side-vpin, so the two series have different lengths)
    pairs = [(v, b) for _ts, v, b in vpin_series if v is not None and b is not None]
    out: dict = {"n_buckets_total": len(vpin_series), "n_vpin_points": len(side)}
    if len(side) == 0:
        return out
    out["distribution"] = {
        f"p{q}": float(np.percentile(side, q)) for q in (50, 90, 95, 97)
    }
    out["max"] = float(np.max(side))
    if len(side) > 1:
        a, b = side[:-1], side[1:]
        denom = np.std(a) * np.std(b)
        out["lag1_autocorr"] = (
            float(np.mean((a - a.mean()) * (b - b.mean())) / denom)
            if denom > 0
            else None
        )
    if len(pairs) > 1:
        pv = np.array([v for v, _b in pairs])
        pb = np.array([b for _v, b in pairs])
        if np.std(pv) > 0 and np.std(pb) > 0:
            out["side_vs_bvc_corr"] = float(np.corrcoef(pv, pb)[0, 1])
    if bucket_minutes:
        arr = np.array(bucket_minutes, dtype=float)
        out["bucket_minutes"] = {
            "median": float(np.median(arr)),
            "min": float(np.min(arr)),
            "max": float(np.max(arr)),
        }
    return out
