"""Unit tests for sel_v2.strategies.inverse_vocab (S2 Step 3 stub completion).

Pins the frozen §6/§14.2 semantics: direction-aware Absorption/Sweep detection and the
Type A/B classification that a non-direction-aware stub kept aborting on.
"""

import numpy as np
import pytest

from sel_v2.strategies.inverse_vocab import (
    AbsorptionSignal,
    SweepSignal,
    adaptive_percentile,
    classify_entry_type,
    detect_absorption,
    detect_sweep,
)


# ── adaptive_percentile ────────────────────────────────────────────────────────


def test_adaptive_percentile_abstains_below_min_obs():
    assert adaptive_percentile([1, 2, 3], 10.0, 0.8, min_obs=30) is None
    assert adaptive_percentile(None, 10.0, 0.8) is None


def test_adaptive_percentile_upper_tail():
    hist = list(range(100))
    assert adaptive_percentile(hist, 95.0, 0.8, min_obs=30) is True  # >= p80
    assert adaptive_percentile(hist, 50.0, 0.8, min_obs=30) is False


def test_adaptive_percentile_lower_tail():
    hist = list(range(100))
    assert adaptive_percentile(hist, 5.0, 0.30, min_obs=30) is True  # <= p30
    assert adaptive_percentile(hist, 80.0, 0.30, min_obs=30) is False


# ── detect_absorption ──────────────────────────────────────────────────────────


def test_absorption_present_when_effort_high_result_low():
    tf_hist = list(np.linspace(0.0, 0.5, 50))  # tf_net 0.9 is well above p80
    pr_hist = list(np.linspace(0.5, 5.0, 50))  # price_response 0.1 is below p30
    sig = detect_absorption(
        taker_net=900.0,
        taker_vol=1000.0,
        price_delta_abs=1.0,
        atr=10.0,
        tf_net_history=tf_hist,
        price_response_history=pr_hist,
    )
    assert sig.present and sig.direction == "up"  # positive net → up absorbed


def test_absorption_direction_down_on_negative_net():
    tf_hist = list(np.linspace(0.0, 0.5, 50))
    pr_hist = list(np.linspace(0.5, 5.0, 50))
    sig = detect_absorption(-900.0, 1000.0, 1.0, 10.0, tf_hist, pr_hist)
    assert sig.present and sig.direction == "down"


def test_absorption_absent_when_price_moved():
    tf_hist = list(np.linspace(0.0, 0.5, 50))
    pr_hist = list(np.linspace(0.5, 5.0, 50))
    # big price move → price_response high → not absorbed
    sig = detect_absorption(900.0, 1000.0, 40.0, 10.0, tf_hist, pr_hist)
    assert not sig.present


def test_absorption_absent_without_atr_or_flow():
    assert not detect_absorption(900.0, 1000.0, 1.0, 0.0).present
    assert not detect_absorption(900.0, 0.0, 1.0, 10.0).present


def test_absorption_abstains_cold_history():
    # no history → percentile abstains → conservative absent
    sig = detect_absorption(900.0, 1000.0, 1.0, 10.0, None, None)
    assert not sig.present


# ── detect_sweep ───────────────────────────────────────────────────────────────


def test_sweep_high_detected():
    vol_hist = list(np.linspace(1.0, 100.0, 50))
    sig = detect_sweep(
        high_48h=100.0,
        low_48h=90.0,
        touch_high=100.05,
        touch_low=99.0,
        touch_volume=99.0,
        reverted_from_high=True,
        reverted_from_low=False,
        volume_history=vol_hist,
    )
    assert sig.present and sig.direction == "up"


def test_sweep_low_detected():
    vol_hist = list(np.linspace(1.0, 100.0, 50))
    sig = detect_sweep(
        high_48h=110.0,
        low_48h=100.0,
        touch_high=101.0,
        touch_low=99.95,
        touch_volume=99.0,
        reverted_from_high=False,
        reverted_from_low=True,
        volume_history=vol_hist,
    )
    assert sig.present and sig.direction == "down"


def test_sweep_absent_without_revert():
    vol_hist = list(np.linspace(1.0, 100.0, 50))
    sig = detect_sweep(100.0, 90.0, 100.05, 99.0, 99.0, False, False, vol_hist)
    assert not sig.present


def test_sweep_absent_low_volume():
    vol_hist = list(np.linspace(1.0, 100.0, 50))
    sig = detect_sweep(100.0, 90.0, 100.05, 99.0, 2.0, True, False, vol_hist)
    assert not sig.present  # volume below p90


# ── classify_entry_type (§14.2) ────────────────────────────────────────────────


def _absorb(direction):
    return AbsorptionSignal(present=True, direction=direction)


def _sweep(direction):
    return SweepSignal(present=True, direction=direction)


def test_type_a_reversal_aligned():
    # CUSUM LONG (up) + absorption up + sweep up → Type A
    assert classify_entry_type("LONG", _absorb("up"), _sweep("up"), None) == "A"


def test_type_a_fails_on_misaligned_direction():
    # absorption/sweep on the wrong side → not Type A
    assert classify_entry_type("LONG", _absorb("down"), _sweep("down"), None) is None
    assert classify_entry_type("LONG", _absorb("up"), _sweep("down"), None) is None


def test_type_b_momentum():
    absent = AbsorptionSignal(present=False)
    no_sweep = SweepSignal(present=False)
    assert classify_entry_type("SHORT", absent, no_sweep, True) == "B"


def test_type_b_blocked_by_absorption():
    # persistent OFI but absorption present → not momentum
    assert (
        classify_entry_type("LONG", _absorb("up"), SweepSignal(present=False), True)
        is None
    )


def test_ambiguous_aborts():
    absent = AbsorptionSignal(present=False)
    no_sweep = SweepSignal(present=False)
    assert classify_entry_type("LONG", absent, no_sweep, None) is None
    assert classify_entry_type("LONG", absent, no_sweep, False) is None
    assert classify_entry_type(None, absent, no_sweep, True) is None
