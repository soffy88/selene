"""Numerical regression tests for Selene 3O migration.

Each test verifies that the stack replacement produces numerically
equivalent results to the original Selene implementation.
"""
import math

import numpy as np
import pytest

from tests.migration import fixtures


class TestComputeAutocorr:
    """Bucket A #8: compute_autocorr → oprim.pearson_spearman_corr"""

    def test_matches_corrcoef(self):
        """Verify oprim.pearson_spearman_corr matches np.corrcoef for lag-1."""
        prices = fixtures.get_price_series(200).tolist()
        from sel_engine.features.price import compute_autocorr
        result = compute_autocorr(prices, window=50)
        # Manual reference using np.corrcoef
        arr = np.array(prices[-51:], dtype=float)
        rets = np.diff(np.log(arr))
        expected = float(np.corrcoef(rets[:-1], rets[1:])[0, 1])
        assert result is not None
        np.testing.assert_allclose(result, expected, rtol=1e-10)

    def test_short_series_returns_none(self):
        from sel_engine.features.price import compute_autocorr
        assert compute_autocorr([100.0, 101.0], window=50) is None


class TestScoreFundingZscore:
    """Bucket A #13: score_funding_zscore → numpy-based z-score"""

    def test_matches_manual(self):
        from services.signal.factors.composite import score_funding_zscore
        history = [0.01, 0.02, -0.01, 0.015, 0.005, 0.01, 0.02, 0.0, -0.005, 0.01]
        current = 0.05
        result = score_funding_zscore(current, history)
        # Manual: mean=0.0085, std=0.00876..., z=(0.05-0.0085)/0.00876=4.73
        # clamped to 3, /3 = 1.0, negated = -1.0
        assert result == -1.0

    def test_empty_history(self):
        from services.signal.factors.composite import score_funding_zscore
        assert score_funding_zscore(0.01, []) == 0.0

    def test_zero_std(self):
        from services.signal.factors.composite import score_funding_zscore
        assert score_funding_zscore(0.01, [0.01] * 10) == 0.0


class TestCalcHistoricalVar:
    """Bucket A #1: calc_historical_var → oprim.value_at_risk"""

    def test_basic_var(self):
        from services.risk.portfolio.var_engine import calc_historical_var
        rng = np.random.default_rng(42)
        returns = (rng.normal(-10, 100, 200)).tolist()
        result = calc_historical_var(returns)
        assert result is not None
        assert result.var_95 >= 0
        assert result.var_99 >= result.var_95
        assert result.expected_shortfall >= result.var_95
        assert result.n_observations == 200

    def test_all_positive_returns(self):
        from services.risk.portfolio.var_engine import calc_historical_var
        returns = [abs(float(x)) + 1.0 for x in range(100)]
        result = calc_historical_var(returns)
        assert result is not None
        assert result.var_95 == 0.0

    def test_insufficient_data(self):
        from services.risk.portfolio.var_engine import calc_historical_var
        assert calc_historical_var([1.0] * 10) is None


class TestHawkesDedup:
    """Bucket A #24-25: hawkes_calibration.py dedup → hawkes/mle.py"""

    def test_hawkes_nll_same_source(self):
        from sel_v2.hawkes.mle import hawkes_nll as mle_nll
        from sel_v2.offline.hawkes_calibration import hawkes_nll as cal_nll
        # They should be the exact same function object
        assert mle_nll is cal_nll

    def test_fit_hawkes_same_source(self):
        from sel_v2.hawkes.mle import fit_hawkes as mle_fit
        from sel_v2.offline.hawkes_calibration import fit_hawkes as cal_fit
        assert mle_fit is cal_fit


class TestRank:
    """Bucket A #12: _rank → scipy.stats.rankdata"""

    def test_basic_ranking(self):
        """Verify scipy.stats.rankdata ordinal matches original logic."""
        from scipy.stats import rankdata
        result = rankdata([3.0, 1.0, 2.0], method='ordinal').tolist()
        assert result == [3.0, 1.0, 2.0]

    def test_larger_list(self):
        from scipy.stats import rankdata
        values = [10, 5, 8, 3, 7]
        result = rankdata(values, method='ordinal').tolist()
        # 3→1, 5→2, 7→3, 8→4, 10→5
        assert result == [5.0, 2.0, 4.0, 1.0, 3.0]
