"""Tests for sel_v2.offline.lens_common (v2.2 Chan/ICT lens batch — shared math).

The zigzag-equivalence test is the gate for the whole lens batch: `zigzag_swings`
must produce exactly the same (start, end, direction) triples as the frozen
`leg_census._zigzag_legs` so the census and the lens studies stay comparable.
"""

import numpy as np
import pytest

from sel_v2.offline.leg_census import _zigzag_legs
from sel_v2.offline.lens_common import (
    SurgingLeg,
    Swing,
    SwingStructure,
    atr_zigzag_swings,
    bh_adjust,
    bootstrap_mean_diff_ci,
    clopper_pearson,
    compute_atr,
    fisher_one_sided,
    pivot_overlap,
    surging_legs,
    swings_confirmed_asof,
    zigzag_swings,
)


# ── zigzag equivalence + causality ───────────────────────────────────────────


def _random_walks():
    for seed in range(5):
        rng = np.random.default_rng(seed)
        yield 100.0 * np.exp(np.cumsum(rng.normal(0, 0.01, size=800)))


def test_zigzag_swings_equivalent_to_frozen_leg_census_zigzag():
    for prices in _random_walks():
        for thresh in (
            lambda i, ext: 0.02 * ext,  # pct threshold
            lambda i, ext: 1.5,  # absolute threshold
        ):
            frozen = _zigzag_legs(prices, thresh)
            ours = zigzag_swings(prices, thresh)
            assert [(s.start_idx, s.end_idx, s.direction) for s in ours] == frozen


def test_confirm_idx_strictly_after_pivot():
    for prices in _random_walks():
        for s in zigzag_swings(prices, lambda i, ext: 0.02 * ext):
            assert s.confirm_idx > s.end_idx


def test_zigzag_degenerate_inputs():
    assert zigzag_swings(np.array([100.0]), lambda i, e: 1.0) == []
    assert zigzag_swings(np.full(10, 100.0), lambda i, e: 1.0) == []


def test_swings_confirmed_asof_causal():
    prices = next(_random_walks())
    swings = zigzag_swings(prices, lambda i, ext: 0.02 * ext)
    assert len(swings) >= 4
    for bar in (swings[1].confirm_idx, swings[3].confirm_idx - 1):
        known = swings_confirmed_asof(swings, bar, k=3)
        assert all(s.confirm_idx <= bar for s in known)
        assert len(known) <= 3


def test_atr_zigzag_uses_atr_at_current_bar():
    prices = next(_random_walks())
    atr = compute_atr(prices * 1.001, prices * 0.999, prices)
    swings = atr_zigzag_swings(prices, atr, mult=1.5)
    frozen = _zigzag_legs(prices, lambda i, ext: 1.5 * atr[i])
    assert [(s.start_idx, s.end_idx, s.direction) for s in swings] == frozen


# ── pivot overlap (CHAN-3) ───────────────────────────────────────────────────


def _swing(start, end, direction):
    return Swing(start_idx=start, end_idx=end, direction=direction, confirm_idx=end + 2)


def test_pivot_overlap_full_partial_zero():
    #        idx: 0    1    2    3
    close = np.array([100.0, 110.0, 102.0, 108.0])
    s1 = _swing(0, 1, 1)  # range [100, 110]
    s2 = _swing(1, 2, -1)  # range [102, 110]
    s3 = _swing(2, 3, 1)  # range [102, 108]
    width, ratio = pivot_overlap([s1, s2, s3], close, atr_i=2.0)
    assert width == pytest.approx(6.0)  # [102, 108]
    assert ratio == pytest.approx(3.0)

    # zero overlap: third swing entirely above the first
    close2 = np.array([100.0, 104.0, 103.0, 120.0, 118.0, 130.0])
    z1 = _swing(0, 1, 1)  # [100, 104]
    z2 = _swing(1, 2, -1)  # [103, 104]
    z3 = _swing(3, 5, 1)  # [120, 130]
    width, ratio = pivot_overlap([z1, z2, z3], close2, atr_i=2.0)
    assert width == 0.0 and ratio == 0.0


def test_pivot_overlap_insufficient_swings_or_bad_atr_is_nan():
    close = np.array([100.0, 110.0])
    w, r = pivot_overlap([_swing(0, 1, 1)], close, atr_i=2.0)
    assert np.isnan(w) and np.isnan(r)
    w, r = pivot_overlap(
        [_swing(0, 1, 1), _swing(0, 1, -1), _swing(0, 1, 1)], close, atr_i=0.0
    )
    assert np.isnan(w) and np.isnan(r)


# ── SwingStructure (ICT-2) ───────────────────────────────────────────────────


def test_swing_structure_up_bos_choch_sequence():
    #                       0     1      2      3      4      5      6
    close = np.array([100.0, 110.0, 105.0, 115.0, 108.0, 116.0, 104.0])
    m = SwingStructure()
    assert m.state == "RANGE"
    # confirmed swings: high@1 (110), low@2 (105), high@3 (115), low@4 (108)
    m.on_swing_confirmed(_swing(0, 1, 1), close)
    m.on_swing_confirmed(_swing(1, 2, -1), close)
    assert m.state == "RANGE"  # needs 2 highs + 2 lows
    m.on_swing_confirmed(_swing(2, 3, 1), close)
    m.on_swing_confirmed(_swing(3, 4, -1), close)
    assert m.state == "UP"  # HH (115>110) + HL (108>105)

    assert m.on_bar(116.0) == "BOS_UP"  # breaks confirmed high 115
    assert m.on_bar(117.0) is None  # same reference — fires once
    assert m.on_bar(104.0) == "CHOCH_DOWN"  # breaks confirmed higher low 108
    assert m.on_bar(103.0) is None  # latched


def test_swing_structure_down_mirror_and_rearm():
    close = np.array([100.0, 90.0, 96.0, 85.0, 92.0, 80.0, 97.0])
    m = SwingStructure()
    m.on_swing_confirmed(_swing(0, 1, -1), close)  # low 90
    m.on_swing_confirmed(_swing(1, 2, 1), close)  # high 96
    m.on_swing_confirmed(_swing(2, 3, -1), close)  # low 85 (LL)
    m.on_swing_confirmed(_swing(3, 4, 1), close)  # high 92 (LH)
    assert m.state == "DOWN"
    assert m.on_bar(84.0) == "BOS_DOWN"
    assert m.on_bar(83.0) is None
    # new lower low confirms → fresh reference re-arms BOS
    m.on_swing_confirmed(_swing(4, 5, -1), close)  # low 80
    assert m.state == "DOWN"  # LH 92<96, LL 80<85
    assert m.on_bar(79.0) == "BOS_DOWN"
    assert m.on_bar(97.0) == "CHOCH_UP"  # breaks lower high 92


def test_swing_structure_range_fires_nothing():
    close = np.array([100.0, 110.0, 95.0, 108.0, 95.5])
    m = SwingStructure()
    m.on_swing_confirmed(_swing(0, 1, 1), close)  # high 110
    m.on_swing_confirmed(_swing(1, 2, -1), close)  # low 95
    m.on_swing_confirmed(_swing(2, 3, 1), close)  # high 108 (LH)
    m.on_swing_confirmed(_swing(3, 4, -1), close)  # low 95.5 (HL) → mixed
    assert m.state == "RANGE"
    assert m.on_bar(120.0) is None
    assert m.on_bar(80.0) is None


# ── surging_legs ─────────────────────────────────────────────────────────────


def test_surging_legs_extraction_direction_and_end_via():
    states = ["Drifting_Calm", "Surging", "Surging", "Drifting_Calm", "Surging"]
    via = [None, None, None, "Exhaustion", None]
    close = np.array([100.0, 100.0, 120.0, 118.0, 117.0])
    legs = surging_legs(states, via, close)
    assert len(legs) == 2
    assert legs[0] == SurgingLeg(
        leg_id=0, start_idx=1, end_idx=2, direction=1, end_via="Exhaustion"
    )
    # tail leg runs to the end: no after-bar → end_via None; flat → direction 0
    assert legs[1].start_idx == 4 and legs[1].end_via is None
    assert legs[1].direction == 0


# ── statistics helpers ───────────────────────────────────────────────────────


def test_fisher_one_sided_known_table():
    # strong enrichment → small p; scipy cross-check by construction
    p_enriched = fisher_one_sided([[9, 1], [1, 9]])
    p_flat = fisher_one_sided([[5, 5], [5, 5]])
    assert p_enriched < 0.005
    assert p_flat > 0.5


def test_clopper_pearson_bounds():
    lo, hi = clopper_pearson(10, 13)
    assert 0.0 < lo < 10 / 13 < hi < 1.0
    assert clopper_pearson(0, 13)[0] == 0.0
    assert clopper_pearson(13, 13)[1] == 1.0


def test_bh_adjust_monotone_and_capped():
    pvals = [0.01, 0.04, 0.03, 0.9]
    q = bh_adjust(pvals)
    # q preserves the p-value ordering and never exceeds 1
    order_p = np.argsort(pvals)
    assert all(
        q[order_p[i]] <= q[order_p[i + 1]] + 1e-12 for i in range(len(pvals) - 1)
    )
    assert all(0 <= x <= 1 for x in q)
    assert q[0] == pytest.approx(0.04)  # 0.01 * 4 / 1


def test_bootstrap_ci_deterministic_and_ordered():
    a = [1.0, 2.0, 3.0, 4.0]
    b = [0.0, 0.5, 1.0]
    ci1 = bootstrap_mean_diff_ci(a, b, n_boot=2000, seed=42)
    ci2 = bootstrap_mean_diff_ci(a, b, n_boot=2000, seed=42)
    assert ci1 == ci2
    assert ci1[0] < ci1[1]
