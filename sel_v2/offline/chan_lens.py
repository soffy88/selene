"""Chan (缠论) lens — pure functions for the CHAN-1/2/3 candidates (v2.2 pool).

CHAN-1 三买回踩:  breakout-retest confirmation over a GEOMETRIC breakout proxy.
                  sel's Release (price breakout + OFI jump + OI acceleration) has
                  fired ZERO times in the 2yr annotation and live history, so this
                  does NOT test sel's Release semantics — the deviation is repeated
                  in every consumer-facing artifact (candidate-pool user ruling).
CHAN-2 背驰:      momentum divergence inside annotated Surging legs.
CHAN-3 中枢重叠:  price-range intersection of the last 3 confirmed zigzag swings.

Pure functions over OHLC arrays + the annotation state series. Offline-only home;
the live observation tools import these same functions (one code path). Touches
nothing under strategies/**, states/**, or the epoch.
"""

from __future__ import annotations

import dataclasses
from typing import Optional

import numpy as np

from sel_v2.offline.lens_common import (
    SurgingLeg,
    atr_zigzag_swings,
    pivot_overlap,
    swings_confirmed_asof,
)

# Both consolidation configs run and both are reported; none picked after seeing
# results (leg_census discipline). (name, min_bars, range_atr_mult)
CONSOLIDATION_CONFIGS = [("K18", 18, 3.0), ("K30", 30, 4.0)]

RETEST_WINDOW_BARS = 6  # 24h of 4H bars — the candidate-pool definition
FWD_RETURN_BARS = 6  # forward-return window measured AFTER the retest window
DIVERGENCE_MOMENTUM_RATIO = 0.7  # candidate-pool definition
OVERLAP_SWINGS = 3


# ── CHAN-1: geometric breakout + retest classification ───────────────────────


@dataclasses.dataclass
class BreakoutEvent:
    idx: int  # bar whose CLOSE first left the range (event time, causal)
    direction: int  # +1 up-breakout, -1 down
    range_high: float
    range_low: float
    range_mid: float
    consolidation_bars: int


@dataclasses.dataclass
class RetestOutcome:
    event: BreakoutEvent
    retest_class: str  # 'A' | 'B' | 'C'
    retest_extreme: float  # min(low) after up-breakout / max(high) after down
    fwd_ret_24h: Optional[float]  # direction-signed log return over bars
    #   [idx+RETEST_WINDOW, idx+RETEST_WINDOW+FWD_RETURN_BARS]; None if the series
    #   ends first (tail truncation — excluded from the hypothesis test, counted)


def detect_breakouts(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    atr: np.ndarray,
    min_bars: int,
    range_mult: float,
) -> list[BreakoutEvent]:
    """Causal consolidation-then-breakout detector.

    A consolidation is 'active' at bar i when the trailing `min_bars` window
    [i-min_bars+1, i] has high-low range <= range_mult * atr[i]. Once active, the
    range is FROZEN at the values it had when the window condition last held, and
    the first subsequent bar whose close falls strictly outside [range_low,
    range_high] is the breakout event. A new consolidation must fully re-form
    (window condition met again after the breakout) before another event can fire
    — no overlapping events."""
    n = len(close)
    events: list[BreakoutEvent] = []
    i = min_bars
    while i < n:
        lo_w = float(np.min(low[i - min_bars + 1 : i + 1]))
        hi_w = float(np.max(high[i - min_bars + 1 : i + 1]))
        if not atr[i] > 0 or (hi_w - lo_w) > range_mult * atr[i]:
            i += 1
            continue
        # consolidation formed at bar i; extend while the window condition holds
        j = i + 1
        range_lo, range_hi, cons_bars = lo_w, hi_w, min_bars
        while j < n:
            lo_j = float(np.min(low[j - min_bars + 1 : j + 1]))
            hi_j = float(np.max(high[j - min_bars + 1 : j + 1]))
            if atr[j] > 0 and (hi_j - lo_j) <= range_mult * atr[j]:
                range_lo, range_hi, cons_bars = lo_j, hi_j, cons_bars + 1
                j += 1
            else:
                break
        # range frozen; scan for the first close strictly outside it
        k = j
        while k < n and range_lo <= close[k] <= range_hi:
            k += 1
        if k < n:
            events.append(
                BreakoutEvent(
                    idx=k,
                    direction=1 if close[k] > range_hi else -1,
                    range_high=range_hi,
                    range_low=range_lo,
                    range_mid=(range_hi + range_lo) / 2.0,
                    consolidation_bars=cons_bars,
                )
            )
        i = k + 1  # a new consolidation must re-form after the event
    return events


def classify_retests(
    events: list[BreakoutEvent],
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
) -> list[RetestOutcome]:
    """A/B/C retest classification per the candidate-pool definition (mirrored for
    down-breakouts). Forward return is measured over the FWD_RETURN_BARS bars
    AFTER the retest window so the classification window and the outcome window
    never overlap (deviation from the pool text's '后续 24H 收益', which would be
    circular — noted in the report)."""
    n = len(close)
    out: list[RetestOutcome] = []
    for ev in events:
        w_end = ev.idx + RETEST_WINDOW_BARS
        if w_end >= n:
            continue  # classification window truncated — event unusable, skip
        if ev.direction == 1:
            retest = float(np.min(low[ev.idx + 1 : w_end + 1]))
            if retest > ev.range_high:
                cls = "A"
            elif retest > ev.range_mid:
                cls = "B"
            else:
                cls = "C"
        else:
            retest = float(np.max(high[ev.idx + 1 : w_end + 1]))
            if retest < ev.range_low:
                cls = "A"
            elif retest < ev.range_mid:
                cls = "B"
            else:
                cls = "C"
        f_end = w_end + FWD_RETURN_BARS
        fwd = ev.direction * float(np.log(close[f_end] / close[w_end])) if f_end < n else None
        out.append(RetestOutcome(event=ev, retest_class=cls, retest_extreme=retest, fwd_ret_24h=fwd))
    return out


# ── CHAN-2: momentum divergence inside Surging legs ──────────────────────────


@dataclasses.dataclass
class DivergenceCandidate:
    bar_idx: int
    leg_id: int
    momentum: float  # |cum log return since leg start| / bars elapsed
    prior_momentum: float  # previous same-direction leg's full-leg momentum
    price_exceeds: bool  # close beyond the prior leg's extreme (always True here)


def detect_divergences(legs: list[SurgingLeg], close: np.ndarray) -> tuple[list[DivergenceCandidate], list[int]]:
    """Per-bar divergence candidates per the pool definition: inside a Surging leg,
    price extends beyond the previous SAME-direction leg's extreme close while the
    current leg's per-bar momentum has decayed below DIVERGENCE_MOMENTUM_RATIO of
    the prior leg's. Returns (candidates, testable_leg_ids) — legs with no prior
    same-direction leg are excluded from the test population (reported)."""
    candidates: list[DivergenceCandidate] = []
    testable: list[int] = []
    last_by_dir: dict[int, SurgingLeg] = {}
    for leg in legs:
        prior = last_by_dir.get(leg.direction) if leg.direction != 0 else None
        # a 1-bar prior leg has no measurable momentum — not a usable reference
        if prior is not None and prior.end_idx > prior.start_idx:
            testable.append(leg.leg_id)
            prior_bars = prior.end_idx - prior.start_idx
            prior_m = abs(float(np.log(close[prior.end_idx] / close[prior.start_idx]))) / prior_bars
            prior_extreme = float(close[prior.end_idx])
            # skip the leg's entry bar: zero bars elapsed → momentum undefined
            for i in range(leg.start_idx + 1, leg.end_idx + 1):
                bars = i - leg.start_idx
                m = abs(float(np.log(close[i] / close[leg.start_idx]))) / bars
                exceeds = close[i] > prior_extreme if leg.direction == 1 else close[i] < prior_extreme
                if exceeds and prior_m > 0 and m < DIVERGENCE_MOMENTUM_RATIO * prior_m:
                    candidates.append(
                        DivergenceCandidate(
                            bar_idx=i,
                            leg_id=leg.leg_id,
                            momentum=m,
                            prior_momentum=prior_m,
                            price_exceeds=True,
                        )
                    )
        if leg.direction != 0:
            last_by_dir[leg.direction] = leg
    return candidates, testable


# ── CHAN-3: pivot-overlap series ─────────────────────────────────────────────


def pivot_overlap_series(close: np.ndarray, atr: np.ndarray) -> np.ndarray:
    """Per-bar causal overlap_ratio: intersection of the last OVERLAP_SWINGS
    confirmed 1.5x-ATR zigzag swings, normalized by the bar's ATR. NaN until
    enough swings have confirmed."""
    swings = atr_zigzag_swings(close, atr)
    out = np.full(len(close), np.nan)
    if not swings:
        return out
    confirms = [s.confirm_idx for s in swings]
    k = 0  # number of swings confirmed as of bar i
    for i in range(len(close)):
        while k < len(swings) and confirms[k] <= i:
            k += 1
        if k >= OVERLAP_SWINGS:
            last3 = swings_confirmed_asof(swings[:k], i, k=OVERLAP_SWINGS)
            _w, ratio = pivot_overlap(last3, close, float(atr[i]))
            out[i] = ratio
    return out


def sigma_pctile_series(close: np.ndarray, vol_window: int = 30) -> np.ndarray:
    """Causal expanding percentile rank of trailing realized vol (std of the last
    `vol_window` log returns). Used only as the CHAN-3a low-σ substitute regime
    (the annotation has zero Coiling bars); NaN during warmup."""
    n = len(close)
    logret = np.diff(np.log(close), prepend=np.log(close[0]))
    vol = np.full(n, np.nan)
    for i in range(vol_window, n):
        vol[i] = float(np.std(logret[i - vol_window + 1 : i + 1]))
    out = np.full(n, np.nan)
    hist: list[float] = []
    for i in range(n):
        if np.isnan(vol[i]):
            continue
        if hist:
            out[i] = sum(1 for v in hist if v <= vol[i]) / len(hist)
        hist.append(vol[i])
    return out
