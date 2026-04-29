"""Unit tests for sel_v2.strategies.hawkes_intensity (H1)."""
import math
import time

import numpy as np
import pytest

from sel_v2.strategies.hawkes_intensity import (
    HawkesIntensityTracker,
    HawkesParams,
    RollingIntensityThreshold,
)


# ── HawkesParams ──────────────────────────────────────────────────────────────

def test_branching_ratio():
    p = HawkesParams(mu=0.1, alpha=0.3, beta=0.6)
    assert abs(p.branching_ratio - 0.5) < 1e-12


def test_branching_ratio_zero_beta():
    p = HawkesParams(mu=0.1, alpha=0.3, beta=0.0)
    assert p.branching_ratio == float("inf")


def test_from_h2_reference():
    p = HawkesParams.from_h2_reference()
    assert p.mu > 0
    assert p.alpha > 0
    assert p.beta > 0
    assert p.branching_ratio < 1.0  # sub-critical


# ── HawkesIntensityTracker ────────────────────────────────────────────────────

def test_baseline_intensity_no_events():
    p = HawkesParams(mu=0.5, alpha=0.3, beta=0.4)
    tracker = HawkesIntensityTracker(p)
    assert abs(tracker.intensity(0.0) - 0.5) < 1e-12


def test_intensity_increases_after_event():
    p = HawkesParams(mu=0.1, alpha=0.5, beta=0.2)
    tracker = HawkesIntensityTracker(p)
    lam_before = tracker.intensity(0.0)
    tracker.add_event(0.0)
    lam_after = tracker.intensity(0.0)
    assert lam_after > lam_before


def test_intensity_decays_to_baseline():
    p = HawkesParams(mu=0.1, alpha=0.5, beta=2.0)
    tracker = HawkesIntensityTracker(p)
    tracker.add_event(0.0)
    lam_right = tracker.intensity(0.01)
    lam_later = tracker.intensity(100.0)   # far in the future
    assert lam_right > lam_later
    assert abs(lam_later - p.mu) < 1e-6   # converges to baseline


def test_intensity_monotone_decaying_after_last_event():
    p = HawkesParams(mu=0.1, alpha=0.5, beta=0.3)
    tracker = HawkesIntensityTracker(p)
    tracker.add_event(0.0)
    tracker.add_event(1.0)
    t_vals = [1.0, 2.0, 5.0, 10.0, 50.0]
    lams = [tracker.intensity(t) for t in t_vals]
    for i in range(len(lams) - 1):
        assert lams[i] >= lams[i + 1], f"Non-monotone decay at index {i}"


def test_add_event_returns_intensity():
    p = HawkesParams(mu=0.2, alpha=0.4, beta=0.5)
    tracker = HawkesIntensityTracker(p)
    lam = tracker.add_event(10.0)
    assert lam > p.mu


def test_out_of_order_event_raises():
    p = HawkesParams(mu=0.1, alpha=0.3, beta=0.4)
    tracker = HawkesIntensityTracker(p)
    tracker.add_event(5.0)
    with pytest.raises(ValueError):
        tracker.add_event(3.0)


def test_reset_clears_state():
    p = HawkesParams(mu=0.1, alpha=0.5, beta=0.3)
    tracker = HawkesIntensityTracker(p)
    tracker.add_event(0.0)
    tracker.add_event(1.0)
    tracker.reset()
    assert abs(tracker.intensity(2.0) - p.mu) < 1e-12


def test_multiple_events_superposition():
    p = HawkesParams(mu=0.1, alpha=0.5, beta=1.0)
    tracker = HawkesIntensityTracker(p)
    # Add 3 events at t=0,1,2 and query at t=2
    tracker.add_event(0.0)
    tracker.add_event(1.0)
    lam_at_2 = tracker.add_event(2.0)
    # Manual: A at t=2 = e^{-1}*(1+e^{-1}*(1+1)) = e^{-1}*(1 + e^{-1}*2)
    e1 = math.exp(-1.0)
    A_expected = e1 * (1.0 + e1 * (1.0 + 1.0))
    lam_expected = p.mu + p.alpha * A_expected
    assert abs(lam_at_2 - lam_expected) < 1e-10


def test_update_params_preserves_consistency():
    p1 = HawkesParams(mu=0.1, alpha=0.3, beta=0.5)
    tracker = HawkesIntensityTracker(p1)
    tracker.add_event(0.0)
    tracker.add_event(1.0)
    tracker.add_event(2.0)
    p2 = HawkesParams(mu=0.2, alpha=0.6, beta=1.0)
    tracker.update_params(p2)
    # After update, intensity should use new params; just check it doesn't crash
    # and is > mu
    lam = tracker.intensity(3.0)
    assert lam >= p2.mu


# ── GMM estimation ────────────────────────────────────────────────────────────

def test_fit_gmm_insufficient_data_fallback():
    params = HawkesIntensityTracker.fit_gmm(np.array([1.0, 2.0]), T=10.0)
    # Should return H2 reference (fallback)
    assert params.mu > 0 and params.alpha > 0 and params.beta > 0


def test_fit_gmm_returns_valid_params():
    rng = np.random.default_rng(42)
    # Simulate event times from Poisson with rate 0.5/sec over 1 hour
    event_times = np.sort(rng.uniform(0, 3600, 500))
    params = HawkesIntensityTracker.fit_gmm(event_times, T=3600.0)
    assert params.mu > 0
    assert params.alpha > 0
    assert params.beta > 0
    assert 0.0 < params.branching_ratio < 2.0  # reasonable range


def test_fit_gmm_estimated_intensity_positive():
    rng = np.random.default_rng(7)
    event_times = np.sort(rng.uniform(0, 7200, 300))
    params = HawkesIntensityTracker.fit_gmm(event_times, T=7200.0)
    tracker = HawkesIntensityTracker(params)
    for et in event_times[:10]:
        tracker.add_event(float(et))
    lam = tracker.intensity(float(event_times[-1]))
    assert lam > 0


# ── RollingIntensityThreshold ────────────────────────────────────────────────

def test_threshold_none_when_cold():
    th = RollingIntensityThreshold(window_seconds=3600, quantile=0.70)
    assert th.threshold(0.0) is None


def test_threshold_above_low_intensity():
    th = RollingIntensityThreshold(window_seconds=3600, quantile=0.70)
    # Add 25 observations with value ~1.0
    for i in range(25):
        th.add(float(i * 10), 1.0)
    # A value of 2.0 should be above 70th pct of [1.0, ..., 1.0]
    assert th.above_threshold(250.0, 2.0)


def test_threshold_below_high_intensity():
    th = RollingIntensityThreshold(window_seconds=3600, quantile=0.70)
    for i in range(25):
        th.add(float(i * 10), 5.0)
    # A value of 0.1 should be below threshold
    assert not th.above_threshold(250.0, 0.1)


def test_threshold_pass_through_when_cold():
    th = RollingIntensityThreshold(window_seconds=3600, quantile=0.70)
    # Only 5 observations — should pass through (return True)
    for i in range(5):
        th.add(float(i), 10.0)
    assert th.above_threshold(10.0, 0.001) is True


def test_threshold_evicts_old_observations():
    th = RollingIntensityThreshold(window_seconds=100.0, quantile=0.70)
    for i in range(25):
        th.add(float(i), 1.0)       # old observations at t=0..24
    for i in range(25):
        th.add(200.0 + float(i), 5.0)  # new observations at t=200..224
    # Old observations (t < 124) should be evicted; threshold based on 5.0 values
    thr = th.threshold(225.0)
    assert thr is not None
    assert abs(thr - 5.0) < 0.01
