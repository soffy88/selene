"""Tests for sel_v2.offline.chan_orthodox (正统缠论分割 + MACD 面积背驰)."""

import numpy as np
import pytest

from sel_v2.offline.chan_orthodox import (
    MIN_APEX_SEPARATION,
    build_strokes,
    detect_fractals,
    detect_macd_divergences,
    macd_histogram,
    merge_inclusion,
    stroke_direction_series,
    stroke_overlap_series,
)
from sel_v2.offline.lens_common import SurgingLeg

# ── inclusion merge ──────────────────────────────────────────────────────────


def test_merge_inclusion_nested_bar_merges_upward_context():
    #      bar0        bar1(up)     bar2 ⊂ bar1   bar3
    high = np.array([10.0, 12.0, 11.5, 13.0])
    low = np.array([9.0, 10.5, 10.8, 11.0])
    bars = merge_inclusion(high, low)
    assert len(bars) == 3  # bar2 folded into bar1
    m = bars[1]
    assert m.high == 12.0 and m.low == pytest.approx(10.8)  # up: max high, max low
    assert m.raw_start == 1 and m.raw_end == 2


def test_merge_inclusion_no_nesting_keeps_all():
    high = np.array([10.0, 11.0, 12.0, 13.0])
    low = np.array([9.0, 10.0, 11.0, 12.0])
    assert len(merge_inclusion(high, low)) == 4


def test_merge_inclusion_downward_context_takes_min():
    high = np.array([13.0, 12.0, 11.8, 10.0])
    low = np.array([12.0, 10.5, 10.8, 9.0])
    bars = merge_inclusion(high, low)  # bar2 ⊂ bar1, context down (12 < 13)
    assert len(bars) == 3
    assert bars[1].high == pytest.approx(11.8) and bars[1].low == pytest.approx(10.5)


# ── fractals + strokes ───────────────────────────────────────────────────────


def _zigzag_ohlc(n_legs=4, leg_len=6, base=100.0, step=2.0):
    """Clean alternating up/down path with distinct extremes and no inclusion."""
    high, low = [], []
    price = base
    for k in range(n_legs):
        d = 1 if k % 2 == 0 else -1
        for _ in range(leg_len):
            price += d * step
            high.append(price + 0.5)
            low.append(price - 0.5)
    return np.array(high), np.array(low)


def test_fractals_and_strokes_on_clean_zigzag():
    high, low = _zigzag_ohlc()
    bars = merge_inclusion(high, low)
    fx = detect_fractals(bars)
    kinds = [f.kind for f in fx]
    assert "top" in kinds and "bottom" in kinds
    strokes = build_strokes(bars, fx)
    assert len(strokes) >= 2
    # alternating directions, apex separation respected, causal confirm
    for a, b in zip(strokes, strokes[1:], strict=False):
        assert a.direction == -b.direction
        assert b.end_m - b.start_m >= MIN_APEX_SEPARATION
    for s in strokes:
        assert s.confirm_raw >= s.end_raw  # knowable only after the closing apex
        assert (s.direction == 1) == (s.end_price > s.start_price)


def test_strokes_too_close_fractals_do_not_form_stroke():
    # tiny 2-bar wiggles: fractals exist but apex separation < MIN → no strokes
    high = np.array([10, 11, 10.2, 11.2, 10.4, 11.4, 10.6], dtype=float)
    low = high - 0.3
    bars = merge_inclusion(high, low)
    fx = detect_fractals(bars)
    strokes = build_strokes(bars, fx)
    assert all(s.end_m - s.start_m >= MIN_APEX_SEPARATION for s in strokes)


def test_stroke_overlap_series_causal_and_comparable():
    high, low = _zigzag_ohlc(n_legs=6)
    close = (high + low) / 2
    bars = merge_inclusion(high, low)
    strokes = build_strokes(bars, detect_fractals(bars))
    atr = np.full(len(close), 1.0)
    series = stroke_overlap_series(strokes, len(close), atr)
    finite = np.isfinite(series)
    if finite.any():
        first = int(np.argmax(finite))
        # nothing before the 3rd stroke's confirm bar
        assert first >= strokes[2].confirm_raw
        assert np.all(series[finite] >= 0)


def test_stroke_direction_series_tracks_trend():
    # rising staircase of strokes → UP once 2 tops + 2 bottoms confirmed
    high, low = [], []
    price = 100.0
    for k in range(8):
        d = 1 if k % 2 == 0 else -1
        leg = 8 if d == 1 else 5  # net-up zigzag (HH + HL)
        for _ in range(leg):
            price += d * 1.0
            high.append(price + 0.4)
            low.append(price - 0.4)
    high, low = np.array(high), np.array(low)
    bars = merge_inclusion(high, low)
    strokes = build_strokes(bars, detect_fractals(bars))
    states = stroke_direction_series(strokes, len(high))
    assert states[0] == "RANGE"
    assert "UP" in states


# ── MACD ─────────────────────────────────────────────────────────────────────


def test_macd_histogram_sign_follows_fresh_trend():
    # flat base then a fresh move: histogram takes the move's sign while the
    # move is young (a constant-log-rate path would converge to ~0 instead —
    # DIF and DEA meet — so "sustained trend" is NOT the right sign fixture)
    flat = np.full(60, 100.0)
    up = np.concatenate([flat, 100.0 * np.exp(np.linspace(0, 0.1, 20))])
    assert macd_histogram(up)[-1] > 0
    down = np.concatenate([flat, 100.0 * np.exp(np.linspace(0, -0.1, 20))])
    assert macd_histogram(down)[-1] < 0


def test_macd_divergence_fires_on_weak_second_leg_only():
    # leg1: strong up; gap; leg2: exceeds leg1 extreme with much weaker momentum
    close = [100.0]
    for _ in range(10):
        close.append(close[-1] * 1.02)  # strong
    top1 = close[-1]
    close += [close[-1] * 0.999, close[-1] * 0.998]
    close.append(top1 * 1.001)  # exceed prior extreme immediately
    for _ in range(11):
        close.append(close[-1] * 1.0008)  # weak grind
    close = np.array(close)
    legs = [
        SurgingLeg(0, 0, 10, 1, "Exhaustion"),
        SurgingLeg(1, 13, len(close) - 1, 1, "Exhaustion"),
    ]
    hist = macd_histogram(close)
    cands, testable = detect_macd_divergences(legs, close, hist)
    assert testable == [1]
    assert cands and all(c.leg_id == 1 for c in cands)
    assert all(c.area_rate < 0.7 * c.prior_area_rate for c in cands)

    # equally-strong second leg → no divergence
    close2 = [100.0]
    for _ in range(10):
        close2.append(close2[-1] * 1.02)
    close2 += [close2[-1] * 0.999, close2[-1] * 0.998]
    for _ in range(12):
        close2.append(close2[-1] * 1.02)
    close2 = np.array(close2)
    legs2 = [
        SurgingLeg(0, 0, 10, 1, "Exhaustion"),
        SurgingLeg(1, 13, len(close2) - 1, 1, "Exhaustion"),
    ]
    cands2, _ = detect_macd_divergences(legs2, close2, macd_histogram(close2))
    assert cands2 == []
