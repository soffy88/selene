"""Tests for sel_v2.offline.chan_lens (v2.2 Chan lens — CHAN-1/2/3 pure functions)."""

import numpy as np
import pytest

from sel_v2.offline.chan_lens import (
    FWD_RETURN_BARS,
    RETEST_WINDOW_BARS,
    classify_retests,
    detect_breakouts,
    detect_divergences,
    pivot_overlap_series,
    sigma_pctile_series,
)
from sel_v2.offline.lens_common import SurgingLeg, compute_atr


def _flat_series(n, level=100.0, wiggle=0.5):
    """Alternating +/- wiggle around level → tight consolidation."""
    close = np.array([level + (wiggle if i % 2 else -wiggle) for i in range(n)])
    return close + 0.2, close - 0.2, close  # high, low, close


# ── CHAN-1: breakout detection ───────────────────────────────────────────────


def test_detect_breakouts_fires_on_close_outside_frozen_range():
    n_cons = 25
    high, low, close = _flat_series(n_cons)
    # ramp away upward after the consolidation
    ramp = np.array([110.0, 112.0, 114.0, 116.0])
    high = np.concatenate([high, ramp + 0.2])
    low = np.concatenate([low, ramp - 0.2])
    close = np.concatenate([close, ramp])
    atr = compute_atr(high, low, close)
    events = detect_breakouts(high, low, close, atr, min_bars=18, range_mult=3.0)
    assert len(events) == 1
    ev = events[0]
    assert ev.direction == 1
    assert close[ev.idx] > ev.range_high  # strictly outside
    assert ev.consolidation_bars >= 18
    assert ev.range_low < 100.0 < ev.range_high


def test_detect_breakouts_close_inside_range_never_fires():
    high, low, close = _flat_series(60)
    atr = compute_atr(high, low, close)
    assert detect_breakouts(high, low, close, atr, 18, 3.0) == []


def test_detect_breakouts_down_direction_mirrored():
    n_cons = 25
    high, low, close = _flat_series(n_cons)
    ramp = np.array([90.0, 88.0, 86.0])
    high = np.concatenate([high, ramp + 0.2])
    low = np.concatenate([low, ramp - 0.2])
    close = np.concatenate([close, ramp])
    atr = compute_atr(high, low, close)
    events = detect_breakouts(high, low, close, atr, 18, 3.0)
    assert len(events) == 1 and events[0].direction == -1


# ── CHAN-1: retest classification ────────────────────────────────────────────


def _mk_retest_case(retest_low, tail_bars=FWD_RETURN_BARS + 1):
    """Up-breakout at idx 25 from range [99.3, 100.7]; the 6 following bars dip to
    `retest_low`, then a measurable forward window."""
    high, low, close = _flat_series(25)
    post_close = [104.0, 103.0, retest_low + 1.0, 103.0, 104.0, 105.0, 106.0]
    post_low = [103.0, retest_low + 0.5, retest_low, 102.5, 103.5, 104.5, 105.5]
    tail = [107.0 + i for i in range(tail_bars)]
    close = np.concatenate([close, post_close, tail])
    low = np.concatenate([low, post_low, [t - 0.5 for t in tail]])
    high = np.concatenate([high, [c + 0.5 for c in post_close], [t + 0.5 for t in tail]])
    atr = compute_atr(high, low, close)
    # post-jump ATR inflation can legitimately re-form a wider consolidation later
    # in this synthetic; the test classifies the known first breakout only
    events = detect_breakouts(high, low, close, atr, 18, 3.0)[:1]
    assert events and events[0].idx == 25
    return classify_retests(events, high, low, close), close


def test_classify_retests_a_b_c():
    (a,), _ = _mk_retest_case(retest_low=102.0)  # stays above range_high ≈ 100.7
    assert a.retest_class == "A"
    (b,), _ = _mk_retest_case(retest_low=100.3)  # inside range, above mid ≈ 100.0
    assert b.retest_class == "B"
    (c,), _ = _mk_retest_case(retest_low=99.5)  # below mid
    assert c.retest_class == "C"
    assert a.fwd_ret_24h is not None and a.fwd_ret_24h > 0


def test_classify_retests_forward_window_after_classification_window():
    (a,), close = _mk_retest_case(retest_low=102.0)
    ev = a.event
    # fwd return spans exactly [idx+RETEST_WINDOW, idx+RETEST_WINDOW+FWD_RETURN]
    w_end = ev.idx + RETEST_WINDOW_BARS
    expected = ev.direction * np.log(close[w_end + FWD_RETURN_BARS] / close[w_end])
    assert a.fwd_ret_24h == pytest.approx(expected, abs=1e-12)


def test_classify_retests_tail_truncation_gives_none_fwd():
    (a,), _ = _mk_retest_case(retest_low=102.0, tail_bars=1)  # fwd window truncated
    assert a.retest_class == "A"
    assert a.fwd_ret_24h is None


# ── CHAN-2: divergence ───────────────────────────────────────────────────────


def _legs_pair(momentum_ratio):
    """Two up Surging legs; the 2nd exceeds the 1st's extreme with per-bar momentum
    = momentum_ratio × the 1st leg's. Bars: leg1 [0..4], gap [5..6], leg2 [7..]."""
    m1 = 0.02  # leg1 momentum per bar
    close = [100.0]
    for _ in range(4):
        close.append(close[-1] * np.exp(m1))
    leg1_end = close[-1]
    close += [close[-1] * 0.999, close[-1] * 0.998]  # gap (non-Surging)
    m2 = momentum_ratio * m1
    # start leg2 above leg1's extreme immediately so price_exceeds holds
    close.append(leg1_end * np.exp(m2))
    for _ in range(5):
        close.append(close[-1] * np.exp(m2))
    close = np.array(close)
    legs = [
        SurgingLeg(leg_id=0, start_idx=0, end_idx=4, direction=1, end_via="Exhaustion"),
        SurgingLeg(
            leg_id=1,
            start_idx=7,
            end_idx=len(close) - 1,
            direction=1,
            end_via="Exhaustion",
        ),
    ]
    return legs, close


def test_divergence_fires_at_low_momentum_not_at_high():
    legs, close = _legs_pair(momentum_ratio=0.5)
    cands, testable = detect_divergences(legs, close)
    assert testable == [1]  # leg 0 has no prior same-direction leg
    assert cands and all(c.leg_id == 1 for c in cands)
    assert all(c.momentum < 0.7 * c.prior_momentum for c in cands)

    legs, close = _legs_pair(momentum_ratio=0.8)
    cands, _ = detect_divergences(legs, close)
    assert cands == []  # 0.8 > 0.7 threshold — no divergence


def test_divergence_needs_price_beyond_prior_extreme():
    legs, close = _legs_pair(momentum_ratio=0.5)
    # push leg2 prices BELOW leg1's extreme → price_exceeds never true
    close2 = close.copy()
    close2[7:] = close[4] * 0.95
    cands, _ = detect_divergences(legs, close2)
    assert cands == []


# ── CHAN-3: overlap + sigma series ───────────────────────────────────────────


def test_pivot_overlap_series_causal_nan_then_values():
    rng = np.random.default_rng(7)
    close = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.01, 400)))
    atr = compute_atr(close * 1.002, close * 0.998, close)
    series = pivot_overlap_series(close, atr)
    assert len(series) == len(close)
    assert np.isnan(series[0])
    finite = np.where(np.isfinite(series))[0]
    assert len(finite) > 0
    # once values start they are non-negative
    assert np.all(series[finite] >= 0)


def test_sigma_pctile_series_bounds_and_warmup():
    rng = np.random.default_rng(3)
    close = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.01, 200)))
    s = sigma_pctile_series(close, vol_window=30)
    assert np.all(np.isnan(s[:30]))
    finite = s[np.isfinite(s)]
    assert len(finite) > 100
    assert finite.min() >= 0.0 and finite.max() <= 1.0
