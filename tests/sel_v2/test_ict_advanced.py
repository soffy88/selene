"""Tests for sel_v2.offline.ict_advanced (ICT-3/4/5/6 preregistered mechanizations)."""

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from sel_v2.offline.ict_advanced import (
    FVG_FILL_WINDOW,
    SWEEP_LOOKBACK,
    detect_fvgs,
    detect_order_blocks,
    detect_sweeps,
    fwd_return,
    killzone_slots,
    slot_stats,
)

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _times(n):
    return [T0 + timedelta(hours=4 * i) for i in range(n)]


# ── ICT-3 killzones ──────────────────────────────────────────────────────────


def test_killzone_slots_and_stats_partition_all_bars():
    n = 61
    times = _times(n)
    close = 100.0 + np.arange(n, dtype=float) * 0.1
    volume = np.ones(n)
    slots = killzone_slots(times)
    assert set(slots) == {0, 4, 8, 12, 16, 20}
    st = slot_stats(times, close, volume)
    assert sum(v["n"] for v in st.values()) == n - 1  # first bar excluded
    for v in st.values():
        assert len(v["absret"]) == v["n"]
        assert np.all(v["absret"] >= 0)


# ── ICT-4 sweep ──────────────────────────────────────────────────────────────


def _flat(n, level=100.0):
    close = np.full(n, level)
    return close + 0.5, close - 0.5, close  # high, low, close


def test_sweep_up_fires_on_failed_breakout_only():
    high, low, close = _flat(SWEEP_LOOKBACK + 2)
    i = SWEEP_LOOKBACK + 1
    # wick above the prior high (100.5) but close back below → sweep
    high[i], close[i], low[i] = 102.0, 100.2, 99.8
    events = detect_sweeps(high, low, close)
    assert len(events) == 1
    ev = events[0]
    assert ev.idx == i and ev.direction == 1 and ev.level == pytest.approx(100.5)

    # held breakout (close above the level) → NOT a sweep
    high2, low2, close2 = _flat(SWEEP_LOOKBACK + 2)
    high2[i], close2[i], low2[i] = 102.0, 101.5, 100.0
    assert detect_sweeps(high2, low2, close2) == []


def test_sweep_down_mirror():
    high, low, close = _flat(SWEEP_LOOKBACK + 2)
    i = SWEEP_LOOKBACK + 1
    low[i], close[i], high[i] = 98.0, 99.8, 100.2
    events = detect_sweeps(high, low, close)
    assert len(events) == 1 and events[0].direction == -1


# ── ICT-5 FVG ────────────────────────────────────────────────────────────────


def test_bullish_fvg_detection_touch_and_fill():
    #                 0      1      2      3      4      5
    high = np.array([101.0, 106.0, 108.0, 107.0, 106.0, 105.5])
    low = np.array([99.0, 103.0, 104.0, 105.0, 103.5, 100.5])
    atr = np.full(6, 2.0)
    events = detect_fvgs(high, low, atr)
    assert len(events) == 1
    ev = events[0]
    # gap between bar0 high (101) and bar2 low (104), completed at bar 2
    assert ev.idx == 2 and ev.direction == 1
    assert ev.gap_top == pytest.approx(104.0) and ev.gap_bottom == pytest.approx(101.0)
    assert ev.touched_at == 4  # low 103.5 re-enters the gap
    assert ev.filled_at == 5  # low 100.5 traverses below gap_bottom


def test_fvg_dust_gap_ignored_and_bearish_mirror():
    high = np.array([101.0, 100.5, 100.2])
    low = np.array([99.0, 98.0, 99.15])
    atr = np.full(3, 2.0)
    # gap = 99.15 - 101.0 < 0 → none; and tiny gaps below 0.1*ATR ignored
    assert detect_fvgs(high, low, atr) == []

    # bearish: bar0 low far above bar2 high
    high2 = np.array([110.0, 104.0, 102.0])
    low2 = np.array([108.0, 101.0, 100.0])
    events = detect_fvgs(high2, low2, np.full(3, 2.0))
    assert len(events) == 1 and events[0].direction == -1
    assert events[0].gap_top == pytest.approx(108.0)
    assert events[0].gap_bottom == pytest.approx(102.0)


def test_fvg_unfilled_within_window_stays_none():
    n = 3 + FVG_FILL_WINDOW + 2
    high = np.full(n, 120.0)
    low = np.full(n, 110.0)
    high[0], low[0] = 101.0, 99.0  # bar0 far below → bullish gap at bar 2
    atr = np.full(n, 2.0)
    (ev,) = detect_fvgs(high, low, atr)
    assert ev.direction == 1 and ev.filled_at is None and ev.touched_at is None


# ── ICT-6 order block ────────────────────────────────────────────────────────


def test_bullish_order_block_detect_and_revisit():
    n = 30
    close = np.full(n, 100.0)
    close[:12] = 100.0
    close[12] = 99.0  # down bar (zone source)
    close[13] = 104.0  # displacement: +5 > 2*ATR(2) AND breaks prior 10-bar high
    close[14:20] = 105.0
    close[20] = 99.5  # revisit dips into the zone
    close[21:] = 103.0
    high = close + 0.5
    low = close - 0.5
    atr = np.full(n, 2.0)
    events = detect_order_blocks(high, low, close, atr)
    assert len(events) >= 1
    ev = events[0]
    assert ev.direction == 1 and ev.ob_idx == 12 and ev.confirm_idx == 13
    assert ev.zone_top == pytest.approx(99.5) and ev.zone_bottom == pytest.approx(98.5)
    assert ev.revisit_idx == 20


def test_no_order_block_without_structure_break():
    n = 30
    close = np.full(n, 100.0)
    close[12] = 99.0
    close[13] = 100.4  # small pop: neither 2*ATR nor structure break
    high, low = close + 0.5, close - 0.5
    assert detect_order_blocks(high, low, close, np.full(n, 2.0)) == []


# ── fwd_return ───────────────────────────────────────────────────────────────


def test_fwd_return_value_and_tail():
    close = np.array([100.0] * 5 + [110.0])
    assert fwd_return(close, 0, bars=5) == pytest.approx(np.log(1.1))
    assert fwd_return(close, 1, bars=5) is None
