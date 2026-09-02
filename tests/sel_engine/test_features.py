"""
Tests for sel_engine Wave 1 feature computations.
All tests use synthetic data only — no DB or Redis connections required.
"""

import importlib.util
import math
from datetime import datetime, timezone

import numpy as np
import pytest

from sel_engine.features.derived import compute_hurst_rs, compute_LV
from sel_engine.features.liquidity import (
    compute_H_change_rate_std,
    compute_H_from_samples,
    compute_orderbook_entropy,
)
from sel_engine.features.price import (
    compute_autocorr,
    compute_price_features,
    compute_sigma_p_d2,
)
from sel_engine.features.schema import FeatureVector
from sel_engine.validator import FeatureValidator

# ── compute_price_features ────────────────────────────────────────────────────


# compute_autocorr/compute_hurst_rs delegate to oprim kernels and swallow the
# missing import into `return None`, so the failure surfaces as
# `assert None is not None`, which the root conftest hook cannot catch.
# Applied per-test: sibling tests in these classes do not need oprim and must
# keep running.
_needs_oprim = pytest.mark.skipif(
    importlib.util.find_spec("oprim") is None,
    reason="private quant stack (oprim) not installed",
)


class TestComputePriceFeatures:
    def test_empty_input(self):
        assert compute_price_features([]) == {}

    def test_single_bar(self):
        result = compute_price_features([50000.0])
        assert result["close"] == 50000.0
        assert result["delta_p_pct"] is None
        assert result["sigma_p_24h"] is None

    def test_two_bars_delta(self):
        result = compute_price_features([50000.0, 51000.0])
        assert result["delta_p_pct"] == pytest.approx(2.0, rel=1e-6)

    def test_negative_delta(self):
        result = compute_price_features([51000.0, 50000.0])
        assert result["delta_p_pct"] < 0

    def test_sigma_requires_25_bars(self):
        closes = [50000.0 + i * 100 for i in range(24)]  # only 24 bars
        result = compute_price_features(closes)
        assert result["sigma_p_24h"] is None

    def test_sigma_computed_with_25_bars(self):
        rng = np.random.default_rng(42)
        closes = list(50000.0 * np.exp(np.cumsum(rng.normal(0, 0.01, 25))))
        result = compute_price_features(closes)
        assert result["sigma_p_24h"] is not None
        assert result["sigma_p_24h"] > 0

    def test_constant_close_zero_delta(self):
        result = compute_price_features([100.0, 100.0])
        assert result["delta_p_pct"] == pytest.approx(0.0)


# ── compute_orderbook_entropy ─────────────────────────────────────────────────


class TestOrderbookEntropy:
    def test_empty_levels(self):
        assert compute_orderbook_entropy([]) == 0.0

    def test_all_zero_size(self):
        levels = [(50000.0, 0.0), (49999.0, 0.0)]
        assert compute_orderbook_entropy(levels) == 0.0

    def test_uniform_distribution_maximum_entropy(self):
        # Uniform → H = log(n)
        n = 10
        levels = [(float(i), 1.0) for i in range(n)]
        H = compute_orderbook_entropy(levels)
        expected = math.log(n)
        assert H == pytest.approx(expected, rel=1e-6)

    def test_concentrated_distribution_low_entropy(self):
        # One level holds 99% of size → low entropy
        levels = [(50000.0, 990.0)] + [(float(i), 1.0) for i in range(10)]
        H_conc = compute_orderbook_entropy(levels)
        levels_uniform = [(float(i), 100.0) for i in range(11)]
        H_uniform = compute_orderbook_entropy(levels_uniform)
        assert H_conc < H_uniform

    def test_single_level_zero_entropy(self):
        levels = [(50000.0, 100.0)]
        assert compute_orderbook_entropy(levels) == pytest.approx(0.0, abs=1e-10)


# ── compute_autocorr ──────────────────────────────────────────────────────────


class TestComputeAutocorr:
    def test_insufficient_data(self):
        closes = [100.0] * 12  # need 13 for window=12
        assert compute_autocorr(closes, 12) is None

    @_needs_oprim
    def test_ar1_positive_autocorr(self):
        # AR(1) log-return process with phi=0.85 → strong positive autocorr in returns
        rng = np.random.default_rng(123)
        n_returns = 60
        phi = 0.85
        log_returns = []
        r = 0.01
        for _ in range(n_returns):
            r = phi * r + rng.normal(0, 0.001)
            log_returns.append(r)
        prices = [100.0]
        for r in log_returns:
            prices.append(prices[-1] * np.exp(r))
        ac = compute_autocorr(prices, 12)
        assert ac is not None
        assert ac > 0.5

    @_needs_oprim
    def test_alternating_series_negative_autocorr(self):
        # Strict alternating up/down log-returns → negative autocorrelation
        p = 100.0
        prices = [p]
        for i in range(60):
            p = p * (1.01 if i % 2 == 0 else 0.99)
            prices.append(p)
        ac = compute_autocorr(prices, 12)
        assert ac is not None
        assert ac < 0

    def test_constant_series_returns_none(self):
        # Constant → zero variance returns → corrcoef is NaN → should return None
        closes = [100.0] * 50
        result = compute_autocorr(closes, 12)
        assert result is None


# ── compute_hurst_rs ──────────────────────────────────────────────────────────


class TestHurstRS:
    def test_insufficient_data(self):
        assert compute_hurst_rs([1.0] * 10) is None

    @_needs_oprim
    def test_trending_higher_than_mean_reverting(self):
        # Strong trend should produce higher H than mean-reverting series
        trend = list(np.arange(1, 101, dtype=float))
        H_trend = compute_hurst_rs(trend)
        # AR(-0.9) mean-reverting
        rng = np.random.default_rng(0)
        phi = -0.9
        mr = []
        x = 0.0
        for _ in range(100):
            x = phi * x + rng.normal(0, 1)
            mr.append(x)
        H_mr = compute_hurst_rs(mr)
        assert H_trend is not None
        assert H_mr is not None
        assert H_trend > H_mr

    @_needs_oprim
    def test_mean_reverting_below_half(self):
        # Strongly alternating series → H < 0.5
        series = [(-1) ** i * float(i) for i in range(1, 101)]
        H = compute_hurst_rs(series)
        assert H is not None
        assert H < 0.5

    @_needs_oprim
    def test_trending_series_above_half(self):
        # Strong trend → H > 0.5
        series = list(np.arange(1, 101, dtype=float))
        H = compute_hurst_rs(series)
        assert H is not None
        assert H > 0.5

    def test_output_clamped_zero_to_one(self):
        rng = np.random.default_rng(1)
        series = list(rng.uniform(0, 100, 60))
        H = compute_hurst_rs(series)
        assert H is None or 0.0 <= H <= 1.0


# ── compute_LV ───────────────────────────────────────────────────────────────


class TestComputeLV:
    def test_none_when_depth_missing(self):
        assert compute_LV(None, 1000.0, 5.0, 3.0) is None

    def test_none_when_spread_missing(self):
        assert compute_LV(1000.0, 1000.0, None, 3.0) is None

    def test_none_when_mean_missing(self):
        assert compute_LV(1000.0, None, 5.0, 3.0) is None

    def test_normal_conditions_low_lv(self):
        # depth same as mean, spread same as mean → LV near 0
        lv = compute_LV(1000.0, 1000.0, 3.0, 3.0)
        assert lv is not None
        assert lv == pytest.approx(0.0, abs=1e-6)

    def test_depth_drop_raises_lv(self):
        # 50% depth drop → depth_score = 0.5
        lv = compute_LV(500.0, 1000.0, 3.0, 3.0)
        assert lv is not None
        assert lv > 0.1

    def test_spread_expansion_raises_lv(self):
        # Spread 3x mean → spread_score = 2.0, clipped; depth normal
        lv = compute_LV(1000.0, 1000.0, 9.0, 3.0)
        assert lv is not None
        assert lv > 0.0

    def test_combined_depth_and_spread_high_lv(self):
        # 70% depth drop + spread 4x mean
        lv = compute_LV(300.0, 1000.0, 12.0, 3.0)
        assert lv is not None
        assert lv > 0.4

    def test_lv_capped_at_one(self):
        # Extreme inputs should not exceed 1.0
        lv = compute_LV(1.0, 1_000_000.0, 1_000_000.0, 1.0)
        assert lv is not None
        assert lv <= 1.0

    def test_zero_mean_depth_returns_none(self):
        assert compute_LV(1000.0, 0.0, 3.0, 3.0) is None


# ── FeatureValidator ──────────────────────────────────────────────────────────


class TestFeatureValidator:
    def _make_fv(self, close=50000.0, delta=1.0, sigma=0.01):
        return FeatureVector(
            time=datetime(2024, 1, 1, tzinfo=timezone.utc),
            symbol="BTCUSDT",
            close=close,
            delta_p_pct=delta,
            sigma_p_24h=sigma,
        )

    def test_empty_vectors(self):
        validator = FeatureValidator()
        report = validator.validate([])
        assert report.total_vectors == 0

    def test_all_present_no_flags(self):
        vectors = [self._make_fv() for _ in range(10)]
        validator = FeatureValidator()
        report = validator.validate(vectors)
        # close and delta_p_pct are fully present → should not be flagged
        assert "close" not in report.flagged_features
        assert "delta_p_pct" not in report.flagged_features

    def test_missing_features_flagged(self):
        # All vectors have H = None → 100% missing → should be flagged
        vectors = [self._make_fv() for _ in range(10)]
        validator = FeatureValidator()
        report = validator.validate(vectors)
        assert "H" in report.flagged_features

    def test_partial_missing_below_threshold_not_flagged(self):
        # 10% missing (1 out of 10) → should NOT be flagged (threshold = 20%)
        vectors = [self._make_fv(delta=None if i == 0 else 1.0) for i in range(10)]
        validator = FeatureValidator()
        report = validator.validate(vectors)
        assert "delta_p_pct" not in report.flagged_features

    def test_outlier_detection(self):
        # Inject one extreme outlier
        vectors = [self._make_fv(close=50000.0) for _ in range(50)]
        vectors[0] = self._make_fv(close=50_000_000.0)  # massive outlier
        validator = FeatureValidator()
        report = validator.validate(vectors)
        stats = report.per_feature["close"]
        assert stats.outlier_count >= 1

    def test_stats_populated(self):
        closes = [50000.0 + i * 10 for i in range(20)]
        vectors = [self._make_fv(close=c) for c in closes]
        validator = FeatureValidator()
        report = validator.validate(vectors)
        stats = report.per_feature["close"]
        assert stats.mean is not None
        assert stats.std is not None
        assert stats.p1 is not None
        assert stats.p99 is not None


# ── compute_H_from_samples ────────────────────────────────────────────────────


class TestComputeHFromSamples:
    def test_empty(self):
        H, count = compute_H_from_samples([])
        assert H is None
        assert count == 0

    def test_normal_samples(self):
        samples = [2.3, 2.5, 2.4, 2.6]
        H, count = compute_H_from_samples(samples)
        assert H == pytest.approx(2.45, rel=1e-4)
        assert count == 4

    def test_filters_none_values(self):
        samples = [2.0, None, 2.4, None]
        H, count = compute_H_from_samples(samples)
        assert count == 2


# ── compute_sigma_p_d2 ────────────────────────────────────────────────────────


class TestSigmaPD2:
    def test_insufficient_history(self):
        assert compute_sigma_p_d2([0.01, 0.02]) is None

    def test_correct_second_difference(self):
        # σ[t]=3, σ[t-1]=2, σ[t-2]=1 → d2 = 3 - 2*2 + 1 = 0
        assert compute_sigma_p_d2([1.0, 2.0, 3.0]) == pytest.approx(0.0)

    def test_accelerating_vol(self):
        # 1, 2, 4 → d2 = 4 - 4 + 1 = 1 (accelerating)
        assert compute_sigma_p_d2([1.0, 2.0, 4.0]) == pytest.approx(1.0)


# ── compute_H_change_rate_std ─────────────────────────────────────────────────


class TestHChangeRateStd:
    def test_insufficient_history(self):
        assert compute_H_change_rate_std([2.0] * 12, window=12) is None

    def test_stable_H_near_zero_std(self):
        # Constant H → zero change rate → std should be 0 (or None from degenerate case)
        H_hist = [2.5] * 14
        result = compute_H_change_rate_std(H_hist, window=12)
        # All change rates are 0 → std is 0 or None
        assert result is None or result == pytest.approx(0.0, abs=1e-10)

    def test_volatile_H_nonzero_std(self):
        rng = np.random.default_rng(7)
        H_hist = list(2.0 + rng.normal(0, 0.5, 14))
        H_hist = [max(0.1, h) for h in H_hist]
        result = compute_H_change_rate_std(H_hist, window=12)
        assert result is not None
        assert result > 0
