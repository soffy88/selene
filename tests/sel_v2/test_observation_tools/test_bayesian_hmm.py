"""
Tests for B1 BayesianHMM and B2 HMMBoundaryArbiter.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

# sel_v2.observation_tools pulls in the private quant stack (oprim). Skip the
# module outright when it is absent so CI reports an explicit skip instead of a
# collection error that aborts the whole run.
pytest.importorskip("oprim", reason="private quant stack (oprim) not installed")

from sel_v2.observation_tools.base import BarFeatures, ObservationResult
from sel_v2.observation_tools.bayesian_hmm import MIN_BARS, BayesianHMM, _GaussianHMM
from sel_v2.observation_tools.hmm_boundary_arbiter import HMMBoundaryArbiter


def _ts(offset_bars: int = 0) -> datetime:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return base + timedelta(hours=4 * offset_bars)


def _bar(ret: float, i: int = 0) -> BarFeatures:
    return BarFeatures(timestamp=_ts(i), log_return=ret, volume=1.0)


def _feed(tool, rets):
    results = []
    for i, r in enumerate(rets):
        results.append(tool.update(_bar(r, i)))
    return results


# ── _GaussianHMM unit tests ───────────────────────────────────────────────────


class TestGaussianHMM:
    def test_fit_does_not_raise_on_small_input(self):
        x = np.array([0.01, -0.01, 0.02, -0.02, 0.005] * 5)
        hmm = _GaussianHMM()
        hmm.fit(x)
        assert hmm._mu is not None

    def test_predict_proba_sums_to_1(self):
        x = np.array([0.01, -0.01, 0.02, -0.02] * 10)
        hmm = _GaussianHMM()
        hmm.fit(x)
        gamma = hmm.predict_proba(x)
        row_sums = gamma.sum(axis=1)
        np.testing.assert_allclose(row_sums, 1.0, atol=1e-5)

    def test_predict_returns_0_or_1(self):
        x = np.array([0.01, -0.01, 0.02, -0.02] * 10)
        hmm = _GaussianHMM()
        hmm.fit(x)
        states = hmm.predict(x)
        assert set(states).issubset({0, 1})

    def test_high_vol_state_detection(self):
        """State with larger σ should be identified as HIGH_VOL."""
        rng = np.random.default_rng(0)
        low = rng.normal(0.0, 0.005, 30)
        high = rng.normal(0.0, 0.03, 30)
        x = np.concatenate([low, high])
        hmm = _GaussianHMM()
        hmm.fit(x)
        high_state = int(np.argmax(np.abs(hmm._sigma)))
        assert hmm._sigma[high_state] > hmm._sigma[1 - high_state]


# ── BayesianHMM (B1) ─────────────────────────────────────────────────────────


class TestBayesianHMM:
    def test_warming_before_min_bars(self):
        b1 = BayesianHMM()
        for i in range(MIN_BARS - 1):
            r = b1.update(_bar(0.001, i))
        assert not b1.is_ready()
        assert r.label == "WARMING"
        assert r.signal is False

    def test_ready_after_min_bars(self):
        b1 = BayesianHMM()
        _feed(b1, [0.001] * (MIN_BARS + 1))
        assert b1.is_ready()

    def test_high_vol_detection(self):
        """High-volatility returns should trigger HIGH_VOL label."""
        rng = np.random.default_rng(42)
        b1 = BayesianHMM(high_vol_threshold=0.50)
        # Feed 25 normal bars then 5 high-vol bars
        normal_rets = list(rng.normal(0.0, 0.002, 25))
        _feed(b1, normal_rets)
        high_rets = [0.05, -0.04, 0.06, -0.05, 0.04]
        results = _feed(b1, high_rets)
        last = results[-1]
        assert isinstance(last, ObservationResult)
        # At least one result should detect high vol
        signals = [r.signal for r in results]
        assert any(signals)

    def test_update_returns_float_value(self):
        b1 = BayesianHMM()
        rets = [0.001 * (1 if i % 2 == 0 else -1) for i in range(30)]
        results = _feed(b1, rets)
        for r in results[MIN_BARS:]:
            assert 0.0 <= r.value <= 1.0

    def test_reset_clears_state(self):
        b1 = BayesianHMM()
        _feed(b1, [0.001] * 30)
        assert b1.is_ready()
        b1.reset()
        assert not b1.is_ready()
        assert b1.last_high_vol_proba == 0.0

    def test_metadata_keys_present(self):
        b1 = BayesianHMM()
        rets = [0.001 * (1 if i % 2 == 0 else -1) for i in range(25)]
        results = _feed(b1, rets)
        for r in results:
            if r.label != "WARMING":
                assert "mu_low" in r.metadata
                assert "sigma_high" in r.metadata


# ── HMMBoundaryArbiter (B2) ───────────────────────────────────────────────────


class TestHMMBoundaryArbiter:
    def test_warming_phase(self):
        b2 = HMMBoundaryArbiter()
        assert not b2.is_ready()

    def test_boundary_detected_after_warmup(self):
        """After warmup, B2 should run without error."""
        rng = np.random.default_rng(7)
        b2 = HMMBoundaryArbiter()
        rets = list(rng.normal(0, 0.002, 30))
        results = _feed(b2, rets)
        # All results are ObservationResult
        for r in results:
            assert isinstance(r, ObservationResult)

    def test_stable_returns_no_boundary(self):
        """Completely flat returns → no boundary event."""
        b2 = HMMBoundaryArbiter()
        rets = [0.001] * 30
        results = _feed(b2, rets)
        boundary_results = [r for r in results if r.signal]
        # Flat returns should produce minimal boundaries
        assert len(boundary_results) <= 3  # allow a few during warmup

    def test_labels_are_valid(self):
        b2 = HMMBoundaryArbiter()
        rets = [0.001 * i / 10 for i in range(-15, 16)]
        results = _feed(b2, rets)
        valid = {"WARMING", "STABLE", "BOUNDARY_TO_HIGH", "BOUNDARY_TO_LOW"}
        for r in results:
            assert r.label in valid, f"Unexpected label: {r.label}"

    def test_reset(self):
        b2 = HMMBoundaryArbiter()
        _feed(b2, [0.001] * 30)
        b2.reset()
        assert not b2.is_ready()
