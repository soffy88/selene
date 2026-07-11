"""Orthodox Chan (缠论正统分割) — 分型/笔/中枢 + MACD-area divergence (2026-07-11 batch).

Mechanizes the czsc/chan.py-style ORTHODOX Chan structure the earlier lens study
proxied with a 1.5x ATR zigzag, so the two segmentations can be compared head to
head on the same data:

  1. K线包含合并 (inclusion merge): direction-dependent merging of contained bars
  2. 顶/底分型 (fractals): local 3-merged-bar extremes
  3. 笔 (strokes, "new stroke" rule): alternating top/bottom fractals with apex
     separation >= 4 merged bars; adjacent same-type fractals keep the extreme.
     Each stroke records confirm_raw_idx — the RAW bar at which it became knowable
     (the bar completing the opposite fractal) — no pivot look-ahead, mirroring
     lens_common.Swing.confirm_idx.
  4. 正统中枢: price intersection of 3 consecutive strokes (same overlap math as
     CHAN-3, different segmentation).
  5. MACD 面积背驰: the canonical divergence metric (A vs C segment histogram
     area) mechanized CAUSALLY as an area-RATE comparison — the retail "wait for
     the completed bar area (or estimate x2)" is not causal. Deliberately mirrors
     chan_lens.detect_divergences (same legs, same 0.7 ratio, same
     price-beyond-prior-extreme trigger) so CHAN-5 differs from CHAN-2 in the
     divergence METRIC only — a controlled comparison.

Pure functions over OHLC arrays. Offline-only; never imported by any live path;
touches nothing under strategies/**, states/**, or the epoch.
"""

from __future__ import annotations

import dataclasses
from typing import Optional

import numpy as np

from sel_v2.offline.lens_common import SurgingLeg

MIN_APEX_SEPARATION = 4  # merged bars between opposite fractal apexes (new-stroke rule)
DIVERGENCE_RATIO = 0.7  # identical to chan_lens.DIVERGENCE_MOMENTUM_RATIO


# ── 1. inclusion merge ───────────────────────────────────────────────────────


@dataclasses.dataclass
class MergedBar:
    high: float
    low: float
    raw_start: int  # first raw bar index folded into this merged bar
    raw_end: int  # last raw bar index (the merge is knowable at this bar's close)


def merge_inclusion(high: np.ndarray, low: np.ndarray) -> list[MergedBar]:
    """Standard czsc 包含处理: when two adjacent bars' ranges nest, merge them —
    upward context takes (max high, max low), downward (min high, min low).
    Direction = relation of the previous two merged bars."""
    bars: list[MergedBar] = []
    for i in range(len(high)):
        h, lo = float(high[i]), float(low[i])
        if not bars:
            bars.append(MergedBar(h, lo, i, i))
            continue
        last = bars[-1]
        if (h <= last.high and lo >= last.low) or (h >= last.high and lo <= last.low):
            up = len(bars) < 2 or last.high > bars[-2].high
            if up:
                last.high, last.low = max(last.high, h), max(last.low, lo)
            else:
                last.high, last.low = min(last.high, h), min(last.low, lo)
            last.raw_end = i
        else:
            bars.append(MergedBar(h, lo, i, i))
    return bars


# ── 2+3. fractals → strokes ──────────────────────────────────────────────────


@dataclasses.dataclass
class Fractal:
    kind: str  # 'top' | 'bottom'
    apex_m: int  # merged-bar index of the apex
    price: float  # apex extreme (high for top, low for bottom)
    confirm_m: int  # merged-bar index completing the 3-bar pattern (apex_m + 1)


@dataclasses.dataclass
class Stroke:
    """One 笔 between two opposite fractal apexes."""

    direction: int  # +1 up (bottom→top), -1 down
    start_m: int  # apex merged index of the opening fractal
    end_m: int  # apex merged index of the closing fractal
    start_raw: int  # raw index of the opening apex bar
    end_raw: int  # raw index of the closing apex bar
    start_price: float
    end_price: float
    confirm_raw: int  # raw bar at which this stroke became knowable


def detect_fractals(bars: list[MergedBar]) -> list[Fractal]:
    out: list[Fractal] = []
    for i in range(1, len(bars) - 1):
        a, b, c = bars[i - 1], bars[i], bars[i + 1]
        if b.high > a.high and b.high > c.high:
            out.append(Fractal("top", i, b.high, i + 1))
        elif b.low < a.low and b.low < c.low:
            out.append(Fractal("bottom", i, b.low, i + 1))
    return out


def build_strokes(bars: list[MergedBar], fractals: list[Fractal]) -> list[Stroke]:
    """New-stroke rule: alternate top/bottom fractals with apex separation >=
    MIN_APEX_SEPARATION merged bars; consecutive same-type fractals keep the more
    extreme one (which may retroactively extend the in-progress stroke — the
    CONFIRMED strokes list only ever contains strokes already closed by a valid
    opposite fractal, so downstream consumers stay causal via confirm_raw)."""
    strokes: list[Stroke] = []
    if not fractals:
        return strokes
    anchor: Optional[Fractal] = None
    for fx in fractals:
        if anchor is None:
            anchor = fx
            continue
        if fx.kind == anchor.kind:
            # same type — keep the extreme (top: higher; bottom: lower)
            better = (fx.kind == "top" and fx.price > anchor.price) or (
                fx.kind == "bottom" and fx.price < anchor.price
            )
            if better:
                if strokes and strokes[-1].end_m == anchor.apex_m:
                    # extend the last confirmed stroke to the new extreme
                    strokes[-1].end_m = fx.apex_m
                    strokes[-1].end_raw = bars[fx.apex_m].raw_end
                    strokes[-1].end_price = fx.price
                    strokes[-1].confirm_raw = bars[fx.confirm_m].raw_end
                anchor = fx
            continue
        if fx.apex_m - anchor.apex_m >= MIN_APEX_SEPARATION:
            strokes.append(
                Stroke(
                    direction=1 if fx.kind == "top" else -1,
                    start_m=anchor.apex_m,
                    end_m=fx.apex_m,
                    start_raw=bars[anchor.apex_m].raw_end,
                    end_raw=bars[fx.apex_m].raw_end,
                    start_price=anchor.price,
                    end_price=fx.price,
                    confirm_raw=bars[fx.confirm_m].raw_end,
                )
            )
            anchor = fx
        # opposite fractal too close: ignore it (does not break the stroke)
    return strokes


# ── 4. orthodox pivot (笔中枢) overlap series ────────────────────────────────


def stroke_overlap_series(
    strokes: list[Stroke], n_raw: int, atr: np.ndarray
) -> np.ndarray:
    """Per-raw-bar causal overlap_ratio of the last 3 CONFIRMED strokes' price
    ranges (正统中枢 construction), normalized by ATR — directly comparable with
    chan_lens.pivot_overlap_series (zigzag-based CHAN-3)."""
    out = np.full(n_raw, np.nan)
    if not strokes:
        return out
    k = 0
    for i in range(n_raw):
        while k < len(strokes) and strokes[k].confirm_raw <= i:
            k += 1
        if k >= 3 and atr[i] > 0:
            last3 = strokes[k - 3 : k]
            los = [min(s.start_price, s.end_price) for s in last3]
            his = [max(s.start_price, s.end_price) for s in last3]
            out[i] = max(0.0, min(his) - max(los)) / float(atr[i])
    return out


def stroke_direction_series(strokes: list[Stroke], n_raw: int) -> list[str]:
    """Per-raw-bar structure state from confirmed stroke pivots — the orthodox
    counterpart of ict_lens.structure_series (HH+HL → UP etc.), so leg-direction
    agreement can be compared against the zigzag baseline."""
    tops: list[float] = []
    bottoms: list[float] = []
    out: list[str] = []
    k = 0
    state = "RANGE"
    for i in range(n_raw):
        while k < len(strokes) and strokes[k].confirm_raw <= i:
            s = strokes[k]
            if s.direction == 1:
                tops.append(s.end_price)
            else:
                bottoms.append(s.end_price)
            if len(tops) >= 2 and len(bottoms) >= 2:
                hh, hl = tops[-1] > tops[-2], bottoms[-1] > bottoms[-2]
                lh, ll = tops[-1] < tops[-2], bottoms[-1] < bottoms[-2]
                state = "UP" if (hh and hl) else "DOWN" if (lh and ll) else "RANGE"
            k += 1
        out.append(state)
    return out


# ── 5. MACD-area divergence (CHAN-5) ─────────────────────────────────────────


def macd_histogram(
    close: np.ndarray, fast: int = 12, slow: int = 26, sig: int = 9
) -> np.ndarray:
    def ema(x, n):
        a = 2.0 / (n + 1)
        out = np.empty_like(x)
        out[0] = x[0]
        for i in range(1, len(x)):
            out[i] = a * x[i] + (1 - a) * out[i - 1]
        return out

    dif = ema(close, fast) - ema(close, slow)
    dea = ema(dif, sig)
    return dif - dea


@dataclasses.dataclass
class MacdDivergence:
    bar_idx: int
    leg_id: int
    area_rate: float  # current push |hist| area per bar
    prior_area_rate: float  # previous same-direction push's full area rate


def detect_macd_divergences(
    legs: list[SurgingLeg], close: np.ndarray, hist: np.ndarray
) -> tuple[list[MacdDivergence], list[int]]:
    """CHAN-5: identical event structure to chan_lens.detect_divergences (same
    legs, same price-beyond-prior-extreme trigger, same 0.7 ratio) but the decay
    metric is the canonical MACD histogram area — as an area RATE (|hist| sum per
    bar) so the comparison is causal (a completed-area comparison would need the
    push to be over, i.e. look-ahead). Returns (candidates, testable_leg_ids)."""
    candidates: list[MacdDivergence] = []
    testable: list[int] = []
    last_by_dir: dict[int, SurgingLeg] = {}
    for leg in legs:
        prior = last_by_dir.get(leg.direction) if leg.direction != 0 else None
        if prior is not None and prior.end_idx > prior.start_idx:
            testable.append(leg.leg_id)
            prior_bars = prior.end_idx - prior.start_idx
            prior_rate = (
                float(np.sum(np.abs(hist[prior.start_idx + 1 : prior.end_idx + 1])))
                / prior_bars
            )
            prior_extreme = float(close[prior.end_idx])
            for i in range(leg.start_idx + 1, leg.end_idx + 1):
                bars_elapsed = i - leg.start_idx
                rate = (
                    float(np.sum(np.abs(hist[leg.start_idx + 1 : i + 1])))
                    / bars_elapsed
                )
                exceeds = (
                    close[i] > prior_extreme
                    if leg.direction == 1
                    else close[i] < prior_extreme
                )
                if exceeds and prior_rate > 0 and rate < DIVERGENCE_RATIO * prior_rate:
                    candidates.append(
                        MacdDivergence(
                            bar_idx=i,
                            leg_id=leg.leg_id,
                            area_rate=rate,
                            prior_area_rate=prior_rate,
                        )
                    )
        if leg.direction != 0:
            last_by_dir[leg.direction] = leg
    return candidates, testable
