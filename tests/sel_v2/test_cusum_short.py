"""Unit tests for sel_v2.strategies.cusum_short."""
import pytest

from sel_v2.strategies.cusum_short import CUSUMShort, CUSUMTrigger


# ── Basic accumulator behaviour ───────────────────────────────────────────────

def test_no_trigger_below_threshold():
    c = CUSUMShort(drift_k=0.5)
    # Send mild positive z — should not trigger (threshold=2.0 cold default)
    result = c.update(0.8)
    assert not result.triggered


def test_positive_accumulates():
    c = CUSUMShort(drift_k=0.5)
    prev_cp = 0.0
    for _ in range(3):
        r = c.update(1.5)
        assert r.cusum_positive >= prev_cp
        prev_cp = r.cusum_positive


def test_negative_accumulates():
    c = CUSUMShort(drift_k=0.5)
    prev_cn = 0.0
    for _ in range(3):
        r = c.update(-1.5)
        assert r.cusum_negative >= prev_cn
        prev_cn = r.cusum_negative


def test_drift_prevents_small_noise_accumulation():
    c = CUSUMShort(drift_k=1.0)
    # z=0.5 < drift_k=1.0 → C+ stays at 0
    for _ in range(10):
        r = c.update(0.5)
    assert r.cusum_positive == 0.0


def test_floor_at_zero():
    c = CUSUMShort(drift_k=0.5)
    c.update(2.0)   # build up C-
    r = c.update(-10.0)   # large negative z, resets C+ to 0
    assert r.cusum_positive == 0.0


# ── Trigger behaviour ─────────────────────────────────────────────────────────

def test_trigger_long_after_sustained_positive():
    """Large enough z should trigger LONG against cold threshold=2.0."""
    c = CUSUMShort(drift_k=0.5)
    # C+ grows by (z - k) = 1.5 - 0.5 = 1.0 per step → need 3 steps for C+ > 2.0
    r = None
    for _ in range(5):
        r = c.update(2.0)
    assert r is not None
    assert r.triggered
    assert r.direction == "LONG"


def test_trigger_short_after_sustained_negative():
    c = CUSUMShort(drift_k=0.5)
    r = None
    for _ in range(5):
        r = c.update(-2.0)
    assert r is not None
    assert r.triggered
    assert r.direction == "SHORT"


def test_both_triggered_resolves_dominant():
    """When both C+ and C- both exceed threshold simultaneously, take the larger."""
    c = CUSUMShort(drift_k=0.5)
    # Manually set state by calling multiple times with alternating large values
    # Just test that a non-triggered result is returned when both are exactly equal
    # (hard to engineer exactly, so test the logic via the method)
    c._c_pos = 3.0
    c._c_neg = 3.0
    r = c.update(0.0)
    # Both were equal before update: after update(0.0):
    # C+ = max(0, 3.0 + 0.0 - 0.5) = 2.5, C- = max(0, 3.0 - 0.0 - 0.5) = 2.5
    # Both are 2.5 — both > 2.0 threshold and equal → not triggered
    assert not r.triggered


# ── Reset behaviour ───────────────────────────────────────────────────────────

def test_reset_positive():
    c = CUSUMShort(drift_k=0.5)
    for _ in range(5):
        c.update(2.0)
    c.reset_positive()
    r = c.update(0.0)
    assert r.cusum_positive == 0.0


def test_reset_negative():
    c = CUSUMShort(drift_k=0.5)
    for _ in range(5):
        c.update(-2.0)
    c.reset_negative()
    r = c.update(0.0)
    assert r.cusum_negative == 0.0


def test_reset_all():
    c = CUSUMShort(drift_k=0.5)
    for _ in range(5):
        c.update(2.0)
    c.reset_all()
    r = c.update(0.0)
    assert r.cusum_positive == 0.0
    assert r.cusum_negative == 0.0


# ── Intensity coefficient ──────────────────────────────────────────────────────

def test_intensity_coeff_positive_on_trigger():
    c = CUSUMShort(drift_k=0.5)
    r = None
    for _ in range(5):
        r = c.update(2.0)
    if r and r.triggered:
        assert r.intensity_coeff > 0


def test_intensity_coeff_capped_at_3():
    c = CUSUMShort(drift_k=0.5)
    # Force very high C+
    c._c_pos = 100.0
    r = c.update(10.0)
    if r.triggered:
        assert r.intensity_coeff <= 3.0


# ── State property ────────────────────────────────────────────────────────────

def test_state_dict_keys():
    c = CUSUMShort()
    s = c.state
    assert set(s.keys()) == {"c_pos", "c_neg", "peak_pos", "peak_neg"}


# ── Cold-start threshold ──────────────────────────────────────────────────────

def test_cold_threshold_is_2():
    c = CUSUMShort()
    r = c.update(0.0)
    assert r.threshold == 2.0
