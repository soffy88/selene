"""Correlation-aware portfolio risk analytics (Phase 5)."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../..'))

import math
from services.risk.portfolio.correlation_risk import (
    correlation_matrix, covariance_matrix, correlated_exposure,
    parametric_var_correlated, stress_test, funding_adjusted_cost,
)


class TestCorrelation:
    def test_perfectly_correlated(self):
        r = {"A": [0.01, -0.02, 0.03, -0.01], "B": [0.02, -0.04, 0.06, -0.02]}  # B = 2A
        syms, corr = correlation_matrix(r)
        i, j = syms.index("A"), syms.index("B")
        assert abs(corr[i][j] - 1.0) < 1e-9
        assert abs(corr[i][i] - 1.0) < 1e-9

    def test_anti_correlated(self):
        r = {"A": [0.01, -0.02, 0.03], "B": [-0.01, 0.02, -0.03]}
        syms, corr = correlation_matrix(r)
        i, j = syms.index("A"), syms.index("B")
        assert abs(corr[i][j] + 1.0) < 1e-9

    def test_tail_alignment_unequal_lengths(self):
        r = {"A": [0.1, 0.01, -0.02, 0.03], "B": [0.01, -0.02, 0.03]}
        syms, cov = covariance_matrix(r)
        assert len(syms) == 2  # shorter series truncates the longer


class TestCorrelatedExposure:
    def test_groups_correlated_same_direction(self):
        # A,B move together (corr~1); C is independent-ish. Long A,B,C.
        r = {"A": [0.01, -0.02, 0.03, -0.01, 0.02],
             "B": [0.011, -0.019, 0.031, -0.009, 0.021],
             "C": [-0.02, 0.03, -0.01, 0.02, -0.03]}
        positions = {"A": 100.0, "B": 100.0, "C": 100.0}
        res = correlated_exposure(positions, r, threshold=0.6)
        # A and B should cluster (200 of 300 gross)
        assert res["max_fraction"] >= 200.0 / 300.0 - 1e-9
        assert set(res["cluster"]) >= {"A", "B"}

    def test_opposite_directions_not_grouped(self):
        r = {"A": [0.01, -0.02, 0.03, -0.01], "B": [0.011, -0.019, 0.031, -0.009]}
        positions = {"A": 100.0, "B": -100.0}  # opposite sides
        res = correlated_exposure(positions, r, threshold=0.6)
        assert res["max_fraction"] == 100.0 / 200.0  # each alone

    def test_empty(self):
        assert correlated_exposure({}, {}, 0.6)["max_fraction"] == 0.0


class TestVaR:
    def test_correlation_increases_var(self):
        # Same marginal vols, but correlated longs carry more portfolio risk than hedged.
        corr_r = {"A": [0.01, -0.02, 0.03, -0.01, 0.02],
                  "B": [0.01, -0.02, 0.03, -0.01, 0.02]}  # identical -> corr 1
        long_both = {"A": 100.0, "B": 100.0}
        hedged = {"A": 100.0, "B": -100.0}
        var_corr = parametric_var_correlated(long_both, corr_r, 0.95)
        var_hedge = parametric_var_correlated(hedged, corr_r, 0.95)
        assert var_corr > var_hedge
        assert var_hedge < 1e-6  # perfectly hedged identical series -> ~0 VaR

    def test_zero_positions(self):
        r = {"A": [0.01, -0.02, 0.03], "B": [0.0, 0.01, -0.01]}
        assert parametric_var_correlated({"A": 0.0, "B": 0.0}, r) == 0.0


class TestStress:
    def test_directional_pnl(self):
        positions = {"BTCUSDT": 1000.0, "ETHUSDT": -500.0}
        scen = {"btc_crash": {"BTCUSDT": -0.2, "ETHUSDT": -0.15}}
        out = stress_test(positions, scen)
        # 1000*-0.2 + (-500)*-0.15 = -200 + 75 = -125
        assert math.isclose(out["btc_crash"], -125.0)

    def test_unshocked_symbol_ignored(self):
        out = stress_test({"A": 100.0, "B": 100.0}, {"s": {"A": -0.1}})
        assert math.isclose(out["s"], -10.0)


class TestFundingCost:
    def test_long_pays_positive_funding(self):
        # 24h hold, 8h funding -> 3 periods; 0.0001/period -> 0.0003 drag added.
        c = funding_adjusted_cost(0.001, 0.0001, hold_hours=24, side="LONG")
        assert math.isclose(c, 0.001 + 0.0003)

    def test_short_favorable_not_negative(self):
        # Short receiving positive funding is favorable; cost must not drop below base.
        c = funding_adjusted_cost(0.001, 0.0001, hold_hours=24, side="SHORT")
        assert math.isclose(c, 0.001)

    def test_no_funding(self):
        assert funding_adjusted_cost(0.001, None, 24, "LONG") == 0.001
