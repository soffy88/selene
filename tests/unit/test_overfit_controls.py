"""Overfitting-control tests (optimization item #16).

Covers the Platt-calibration holdout gate and adds coverage for previously
untested pure modules: ic_health and hawkes_critical.
"""
import math

import numpy as np

from services.signal.factors.composite import (
    platt_fit, DEFAULT_CALIBRATION, _platt_grid_fit, _platt_nll)
from services.signal.ic_health import ic_health_scalar
from sel_v2.states.hawkes_critical import compute_hawkes_branching_ratio


# ── Platt calibration holdout (item #16) ──
def test_platt_holdout_never_worse_than_default():
    # The gate's contract: whatever it returns must not be worse than the default
    # on the held-out tail (it adopts the fit only if it beats default there).
    rng = np.random.default_rng(0)
    scores = list(rng.uniform(-1, 1, 200))
    outcomes = list(rng.integers(0, 2, 200))
    c, s = platt_fit(scores, outcomes)
    cut = int(len(scores) * 0.7)
    te_s, te_o = scores[cut:], outcomes[cut:]
    returned_nll = _platt_nll(te_s, te_o, c, s)
    default_nll = _platt_nll(te_s, te_o, *DEFAULT_CALIBRATION)
    assert returned_nll <= default_nll + 1e-9


def test_platt_accepts_real_signal():
    # Outcome is genuinely a sigmoid of the score → fit should generalise and be
    # adopted (non-default), with a positive scale.
    rng = np.random.default_rng(1)
    scores = list(rng.uniform(-1, 1, 300))
    outcomes = [1 if (1 / (1 + math.exp(-(x) * 4))) > rng.uniform() else 0 for x in scores]
    c, s = platt_fit(scores, outcomes)
    assert (c, s) != DEFAULT_CALIBRATION
    assert s > 0


def test_platt_small_sample_plain_fit():
    # < 40 samples → legacy plain in-sample fit path (no holdout), still returns a tuple.
    scores = [(-1) ** i * 0.3 for i in range(25)]
    outcomes = [i % 2 for i in range(25)]
    out = platt_fit(scores, outcomes)
    assert isinstance(out, tuple) and len(out) == 2


def test_platt_too_few_returns_default():
    assert platt_fit([0.1, 0.2], [1, 0]) == DEFAULT_CALIBRATION


# ── ic_health (previously untested) ──
def test_ic_health_neutral_until_min_trades():
    assert ic_health_scalar(0.0, n=5) == 1.0     # too few outcomes
    assert ic_health_scalar(None, n=999) == 1.0  # no IC yet


def test_ic_health_full_when_strong():
    assert ic_health_scalar(0.10, n=50, good=0.05) == 1.0


def test_ic_health_floor_when_dead():
    assert ic_health_scalar(-0.1, n=50, floor=0.0, min_scale=0.25) == 0.25


def test_ic_health_interpolates():
    val = ic_health_scalar(0.025, n=50, good=0.05, floor=0.0, min_scale=0.25)
    assert 0.25 < val < 1.0


# ── hawkes_critical (previously untested) ──
def test_hawkes_branching_insufficient_data_none():
    assert compute_hawkes_branching_ratio(np.zeros(10)) is None


def test_hawkes_branching_flat_series_none():
    # zero variance → no events → None
    assert compute_hawkes_branching_ratio(np.zeros(600)) is None


def test_hawkes_branching_returns_bounded_or_none():
    rng = np.random.default_rng(7)
    lr = rng.standard_normal(600) * 0.01
    # inject clustered shocks so events exist
    lr[100:110] += 0.1
    br = compute_hawkes_branching_ratio(lr)
    assert br is None or (0.0 <= br <= 10.0)
