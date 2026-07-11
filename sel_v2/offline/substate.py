"""Surging sub-state machine (Wave V22-A, offline gate — v2.2 design draft D3).

Wiki-ruled (2026-07-11) sub-state machine for the internal structure of the
**Surging** parent state: three sub-states (Impulse / Pullback / Consolidation)
driven by a causal zigzag (1.5x ATR reversal), three events (RE_PUSH /
STRUCTURE_BREAK / TERMINAL_FLAG), and a moving structural stop (most recent
*confirmed* higher low for a long segment, mirrored to a lower high for a
short segment — ratcheted only on RE_PUSH confirmation, per D3).

Pure function over OHLC arrays + the parent state label series (as read from
`v2_state_annotation`). Never imported by any live path — offline-only, per
the Wave's red line (does not touch strategies/**, states/**, or the epoch).

Frozen parameters (Wiki-ruled, do not retune on replay results):
  zigzag reversal      = 1.5 x ATR(14)
  pullback threshold   = 0.5 x ATR(14)
  consolidation        = 6 bars, range < 1.5 x ATR(14)
  impulse              = distance from running extreme < 1.0 x ATR(14)
  terminal path A      = 2 consecutive push legs with decreasing magnitude
  terminal path B      = latest pullback leg > 1.5 x median(prior pullback legs)
"""

from __future__ import annotations

import dataclasses
import enum
from typing import Optional

import numpy as np
import pandas as pd

ZIGZAG_ATR_MULT = 1.5
PULLBACK_ATR_MULT = 0.5
CONSOLIDATION_BARS = 6
CONSOLIDATION_ATR_MULT = 1.5
IMPULSE_ATR_MULT = (
    1.0  # documents the Impulse definition; not a separate branch (see _classify)
)
TERMINAL_PULLBACK_MULT = 1.5
ATR_WINDOW = 14  # matches sel_v2/paper/strategy_engine.py:_ATR_WINDOW

SURGING_STATE = "Surging"


class SubState(str, enum.Enum):
    IMPULSE = "Impulse"
    PULLBACK = "Pullback"
    CONSOLIDATION = "Consolidation"


class Event(str, enum.Enum):
    RE_PUSH = "RE_PUSH"
    STRUCTURE_BREAK = "STRUCTURE_BREAK"
    TERMINAL_FLAG = "TERMINAL_FLAG"


@dataclasses.dataclass
class BarSubState:
    """One bar's sub-state-machine output. All None/0/False outside Surging."""

    sub_state: Optional[SubState] = None
    direction: Optional[int] = None  # +1 long-side segment, -1 short-side segment
    push_count: int = (
        0  # confirmed RE_PUSH count since the last reset (segment start or break)
    )
    stop_level: Optional[float] = None  # current moving structural stop price
    terminal_flag: bool = (
        False  # latched for the remainder of the current push-structure
    )
    events: list = dataclasses.field(default_factory=list)


def compute_atr(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, window: int = ATR_WINDOW
) -> np.ndarray:
    """True range, rolling mean — identical definition to strategy_engine.py's ATR."""
    prev_close = np.concatenate([[close[0]], close[:-1]])
    tr = np.maximum.reduce(
        [high - low, np.abs(high - prev_close), np.abs(low - prev_close)]
    )
    return pd.Series(tr).rolling(window, min_periods=1).mean().to_numpy()


def _segments(
    parent_state: list[str], target: str = SURGING_STATE
) -> list[tuple[int, int]]:
    """[start, end) bar-index ranges of contiguous runs of `target` in parent_state."""
    segs = []
    i, n = 0, len(parent_state)
    while i < n:
        if parent_state[i] == target:
            j = i
            while j < n and parent_state[j] == target:
                j += 1
            segs.append((i, j))
            i = j
        else:
            i += 1
    return segs


def _classify(
    retracement: float,
    atr_i: float,
    i: int,
    start: int,
    high: np.ndarray,
    low: np.ndarray,
) -> SubState:
    if not np.isnan(atr_i) and retracement >= PULLBACK_ATR_MULT * atr_i:
        return SubState.PULLBACK
    if not np.isnan(atr_i) and (i - start + 1) >= CONSOLIDATION_BARS:
        w_high = high[i - CONSOLIDATION_BARS + 1 : i + 1].max()
        w_low = low[i - CONSOLIDATION_BARS + 1 : i + 1].min()
        if (w_high - w_low) < CONSOLIDATION_ATR_MULT * atr_i:
            return SubState.CONSOLIDATION
    return (
        SubState.IMPULSE
    )  # distance from extreme < 1.0x ATR, or ATR undefined at bar 0


class _Structure:
    """Mutable per-leg bookkeeping, reset at segment start AND on every STRUCTURE_BREAK.

    A STRUCTURE_BREAK does not stop the sub-state machine (the parent Surging segment
    may continue) — it re-arms a fresh structural attempt from that bar, which is what
    makes the Part-3 STRUCTURE_BREAK false-positive metric ("does the leg recover and
    make a new extreme") a meaningful, well-defined question rather than an undefined one.
    """

    def __init__(self, direction: int, base_price: float, base_idx: int):
        self.direction = direction
        self.phase_up = (
            True  # True: extending toward a new push extreme; False: retracing
        )
        self.ext_price = base_price
        self.ext_idx = base_idx
        self.leg_start_price = base_price  # price the CURRENT push leg is measured from
        self.prior_push_pivot: Optional[float] = (
            None  # confirmed push extreme before the active pullback
        )
        self.push_confirmed_this_leg = (
            False  # RE_PUSH already fired for the active push leg
        )
        self.stop_level: Optional[float] = None
        self.pending_low: Optional[float] = (
            None  # confirmed pullback low, awaiting RE_PUSH to become the stop
        )
        self.push_count = 0
        self.terminal_flag = False
        self.push_leg_sizes: list[float] = []
        self.pullback_leg_sizes: list[float] = []

    def _check_terminal(self) -> bool:
        if len(self.push_leg_sizes) >= 3:
            a, b, c = (
                self.push_leg_sizes[-3],
                self.push_leg_sizes[-2],
                self.push_leg_sizes[-1],
            )
            if c < b < a:
                return True
        if len(self.pullback_leg_sizes) >= 2:
            prior = self.pullback_leg_sizes[:-1]
            if prior and self.pullback_leg_sizes[-1] > TERMINAL_PULLBACK_MULT * float(
                np.median(prior)
            ):
                return True
        return False

    def step(self, i: int, close_i: float, atr_i: float) -> tuple[float, list]:
        """Advance one bar. Returns (retracement_from_extreme, events_this_bar)."""
        events: list = []
        d = self.direction

        if self.phase_up:
            if d * (close_i - self.ext_price) > 0:
                self.ext_price, self.ext_idx = close_i, i
                # RE_PUSH: close resumes beyond the prior confirmed push pivot.
                if (
                    not self.push_confirmed_this_leg
                    and self.prior_push_pivot is not None
                    and d * (close_i - self.prior_push_pivot) > 0
                ):
                    self.push_count += 1
                    self.push_confirmed_this_leg = True
                    events.append(Event.RE_PUSH)
                    if (
                        self.stop_level is None
                        or d * (self.pending_low - self.stop_level) > 0
                    ):
                        self.stop_level = self.pending_low
            else:
                retr = d * (self.ext_price - close_i)
                if not np.isnan(atr_i) and retr >= ZIGZAG_ATR_MULT * atr_i:
                    # Confirm the just-completed push pivot; flip into retracement tracking.
                    leg_size = d * (self.ext_price - self.leg_start_price)
                    self.push_leg_sizes.append(leg_size)
                    self.prior_push_pivot = self.ext_price
                    self.push_confirmed_this_leg = False
                    self.phase_up = False
                    self.ext_price, self.ext_idx = close_i, i
        else:
            if d * (self.ext_price - close_i) > 0:
                self.ext_price, self.ext_idx = close_i, i
            else:
                recover = d * (close_i - self.ext_price)
                if not np.isnan(atr_i) and recover >= ZIGZAG_ATR_MULT * atr_i:
                    pullback_size = d * (self.prior_push_pivot - self.ext_price)
                    self.pullback_leg_sizes.append(pullback_size)
                    self.pending_low = self.ext_price
                    self.leg_start_price = self.ext_price
                    self.phase_up = True
                    self.ext_price, self.ext_idx = close_i, i

        # Distance from "the extreme" for sub-state classification: the running push
        # high while actively pushing, or the last CONFIRMED push high while retracing
        # (not the in-progress low being tracked — that would read as ~0 mid-pullback).
        extreme_ref = self.ext_price if self.phase_up else self.prior_push_pivot
        retracement = (
            max(0.0, d * (extreme_ref - close_i)) if extreme_ref is not None else 0.0
        )

        if not self.terminal_flag and self._check_terminal():
            self.terminal_flag = True
            events.append(Event.TERMINAL_FLAG)

        if self.stop_level is not None and d * (close_i - self.stop_level) < 0:
            events.append(Event.STRUCTURE_BREAK)
            self._reset_after_break(i, close_i)

        return retracement, events

    def _reset_after_break(self, i: int, close_i: float) -> None:
        self.phase_up = True
        self.ext_price, self.ext_idx = close_i, i
        self.leg_start_price = close_i
        self.prior_push_pivot = None
        self.push_confirmed_this_leg = False
        self.stop_level = None
        self.pending_low = None
        self.push_count = 0
        self.terminal_flag = False
        self.push_leg_sizes = []
        self.pullback_leg_sizes = []


def compute_substates(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, parent_state: list[str]
) -> list[BarSubState]:
    """Per-bar sub-state-machine output over the full bar series.

    high/low/close: 1-D float arrays, one entry per bar, aligned with parent_state.
    parent_state: the frozen state machine's per-bar label (from v2_state_annotation).
    """
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    close = np.asarray(close, dtype=float)
    n = len(close)
    atr = compute_atr(high, low, close)
    out = [BarSubState() for _ in range(n)]

    for start, end in _segments(parent_state):
        lookback = max(0, start - 3)
        direction = 1 if close[start] >= close[lookback] else -1
        st = _Structure(direction, close[start], start)
        for i in range(start, end):
            retracement, events = st.step(i, close[i], atr[i])
            sub_state = _classify(retracement, atr[i], i, start, high, low)
            out[i] = BarSubState(
                sub_state=sub_state,
                direction=direction,
                push_count=st.push_count,
                stop_level=st.stop_level,
                terminal_flag=st.terminal_flag,
                events=events,
            )
    return out
