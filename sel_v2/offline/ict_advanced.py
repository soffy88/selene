"""ICT advanced concepts — mechanized, PREREGISTERED (2026-07-11 batch).

Mechanizes the four data-ready ICT/SMC core concepts the 2024-2026 scan surfaced
(zero peer review anywhere — one practitioner backtest and retail claims like
"FVG fills 70%" / "Silver Bullet 70-80% win rate", all unverified; the only
academically grounded angle, liquidation-cluster stop hunts, is PENDING our own
liquidation data accumulating — v2_liquidations starts 2026-07-06):

  ICT-3 Killzones      — 4H time-of-day seasonality (case 9 of the PA/ICT case
                         library, priority raised by Amberdata-2025 orderbook
                         evidence; never tested on our data until now)
  ICT-4 Liquidity sweep — failed breakout ("turtle soup"): intrabar break of the
                         prior N-bar extreme that closes back inside → reversal?
  ICT-5 FVG            — 3-bar imbalance gap; fill tracking + first-touch support
  ICT-6 Order Block    — last opposite bar before a displacement + structure
                         break; zone revisit reaction

EVERY parameter below is preregistered — single spec, no variants, chosen before
seeing any result (leg_census discipline). All event indices are causal (an
event exists at the bar that completes its definition).

Pure functions over OHLC arrays; offline-only; never imported by any live
decision path; touches nothing under strategies/**, states/**, or the epoch.
"""

from __future__ import annotations

import dataclasses
from typing import Optional

import numpy as np

# ── PREREGISTERED PARAMETERS (single spec — no shopping) ─────────────────────
SWEEP_LOOKBACK = 30  # prior extreme window: 30 x 4H = 5 days
FWD_BARS = 6  # forward outcome window (24h), same as the lens studies
FVG_MIN_ATR = 0.1  # ignore dust gaps below 0.1 x ATR
FVG_FILL_WINDOW = 30  # bars allowed for a full gap fill
OB_DISPLACEMENT_ATR = 2.0  # displacement: >= 2 x ATR net move ...
OB_DISPLACEMENT_BARS = 3  # ... within <= 3 bars
OB_STRUCTURE_LOOKBACK = 10  # displacement must also break the prior 10-bar extreme
OB_REVISIT_WINDOW = 60  # bars allowed for the zone revisit


# ── ICT-3 Killzones ──────────────────────────────────────────────────────────


def killzone_slots(times) -> np.ndarray:
    """UTC 4H-slot label per bar (0,4,8,12,16,20)."""
    return np.array([t.hour for t in times])


def slot_stats(times, close: np.ndarray, volume: np.ndarray) -> dict:
    """Per-slot |log return| and volume samples (for KW / MW tests upstream)."""
    slots = killzone_slots(times)
    logret = np.abs(np.diff(np.log(close), prepend=np.log(close[0])))
    out: dict[int, dict] = {}
    for s in (0, 4, 8, 12, 16, 20):
        m = slots == s
        m[0] = False  # first bar's return is degenerate
        out[s] = {
            "absret": logret[m],
            "volume": volume[m],
            "n": int(m.sum()),
        }
    return out


# ── ICT-4 liquidity sweep (turtle soup) ──────────────────────────────────────


@dataclasses.dataclass
class SweepEvent:
    idx: int
    direction: int  # +1 swept the prior HIGH (bearish reversal expected), -1 low
    level: float  # the swept prior extreme


def detect_sweeps(
    high: np.ndarray, low: np.ndarray, close: np.ndarray
) -> list[SweepEvent]:
    """Sweep-up at bar i: high[i] breaks the prior SWEEP_LOOKBACK-bar high but the
    CLOSE comes back below it (failed breakout / stop run). Mirrored for lows.
    Note the deliberate contrast with CHAN-1's breakout (close-based, held)."""
    out: list[SweepEvent] = []
    n = len(close)
    for i in range(SWEEP_LOOKBACK, n):
        prior_hi = float(np.max(high[i - SWEEP_LOOKBACK : i]))
        prior_lo = float(np.min(low[i - SWEEP_LOOKBACK : i]))
        if high[i] > prior_hi and close[i] < prior_hi:
            out.append(SweepEvent(i, 1, prior_hi))
        elif low[i] < prior_lo and close[i] > prior_lo:
            out.append(SweepEvent(i, -1, prior_lo))
    return out


# ── ICT-5 fair value gap ─────────────────────────────────────────────────────


@dataclasses.dataclass
class FVGEvent:
    idx: int  # bar completing the 3-bar pattern (causal event time)
    direction: int  # +1 bullish gap (up-imbalance), -1 bearish
    gap_top: float
    gap_bottom: float
    filled_at: Optional[int] = None  # bar of FULL fill within FVG_FILL_WINDOW
    touched_at: Optional[int] = None  # first re-entry into the gap after idx


def detect_fvgs(high: np.ndarray, low: np.ndarray, atr: np.ndarray) -> list[FVGEvent]:
    """Bullish FVG at bar i: low[i] > high[i-2] with gap >= FVG_MIN_ATR x ATR[i]
    (bar i-1 is the displacement candle). Fill/touch tracked forward:
    touch = price re-enters the gap; full fill = price traverses it entirely."""
    out: list[FVGEvent] = []
    n = len(high)
    for i in range(2, n):
        if not atr[i] > 0:
            continue
        if low[i] - high[i - 2] >= FVG_MIN_ATR * atr[i]:
            ev = FVGEvent(i, 1, gap_top=float(low[i]), gap_bottom=float(high[i - 2]))
        elif low[i - 2] - high[i] >= FVG_MIN_ATR * atr[i]:
            ev = FVGEvent(i, -1, gap_top=float(low[i - 2]), gap_bottom=float(high[i]))
        else:
            continue
        for j in range(i + 1, min(i + 1 + FVG_FILL_WINDOW, n)):
            if ev.direction == 1:
                if ev.touched_at is None and low[j] < ev.gap_top:
                    ev.touched_at = j
                if low[j] <= ev.gap_bottom:
                    ev.filled_at = j
                    break
            else:
                if ev.touched_at is None and high[j] > ev.gap_bottom:
                    ev.touched_at = j
                if high[j] >= ev.gap_top:
                    ev.filled_at = j
                    break
        out.append(ev)
    return out


# ── ICT-6 order block ────────────────────────────────────────────────────────


@dataclasses.dataclass
class OrderBlockEvent:
    ob_idx: int  # the opposite bar (zone source)
    confirm_idx: int  # bar completing displacement + structure break (causal)
    direction: int  # +1 bullish OB (expect support on revisit)
    zone_top: float
    zone_bottom: float
    revisit_idx: Optional[int] = None  # first zone touch after confirm_idx


def detect_order_blocks(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, atr: np.ndarray
) -> list[OrderBlockEvent]:
    """Bullish OB: bar b closes down (close[b] < close[b-1] — no open column in
    the joined frame, preregistered proxy), then within OB_DISPLACEMENT_BARS the
    close gains >= OB_DISPLACEMENT_ATR x ATR[b] AND exceeds the prior
    OB_STRUCTURE_LOOKBACK-bar high (structure break). Zone = bar b's range.
    Revisit = first low <= zone_top within OB_REVISIT_WINDOW after confirmation.
    Mirrored for bearish. Overlapping candidates: a bar can source one OB only;
    scanning resumes after the confirmation bar."""
    out: list[OrderBlockEvent] = []
    n = len(close)
    b = max(1, OB_STRUCTURE_LOOKBACK)
    while b < n - 1:
        made = False
        for direction in (1, -1):
            if direction == 1 and not close[b] < close[b - 1]:
                continue
            if direction == -1 and not close[b] > close[b - 1]:
                continue
            struct_hi = float(np.max(high[b - OB_STRUCTURE_LOOKBACK : b]))
            struct_lo = float(np.min(low[b - OB_STRUCTURE_LOOKBACK : b]))
            for j in range(b + 1, min(b + 1 + OB_DISPLACEMENT_BARS, n)):
                moved = (close[j] - close[b]) * direction
                broke = close[j] > struct_hi if direction == 1 else close[j] < struct_lo
                if moved >= OB_DISPLACEMENT_ATR * atr[b] and broke:
                    ev = OrderBlockEvent(
                        ob_idx=b,
                        confirm_idx=j,
                        direction=direction,
                        zone_top=float(high[b]),
                        zone_bottom=float(low[b]),
                    )
                    for k in range(j + 1, min(j + 1 + OB_REVISIT_WINDOW, n)):
                        touched = (
                            low[k] <= ev.zone_top
                            if direction == 1
                            else high[k] >= ev.zone_bottom
                        )
                        if touched:
                            ev.revisit_idx = k
                            break
                    out.append(ev)
                    b = j  # no overlapping OBs from the same impulse
                    made = True
                    break
            if made:
                break
        b += 1
    return out


# ── shared outcome helper ────────────────────────────────────────────────────


def fwd_return(close: np.ndarray, i: int, bars: int = FWD_BARS) -> Optional[float]:
    """Signed log return over the `bars` bars after bar i; None at the tail."""
    if i + bars >= len(close):
        return None
    return float(np.log(close[i + bars] / close[i]))
