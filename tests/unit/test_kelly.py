"""Unit tests for Kelly + Risk Parity. All formulas verified by hand."""
import math, pytest, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../..'))
from services.portfolio.capital.kelly import (
    kelly_fraction, position_size_from_kelly, risk_parity_weights, CapitalAllocator
)

class TestKellyFraction:
    def test_positive_ev(self):
        # p=0.60, b=1.5 → full f*=(0.60*1.5-0.40)/1.5=0.333; half=0.167
        assert abs(kelly_fraction(0.60, 1.5, 0.5) - 0.1667) < 0.001
    def test_negative_ev_zero(self):
        assert kelly_fraction(0.40, 1.0, 0.5) == 0.0
    def test_break_even_zero(self):
        assert kelly_fraction(0.50, 1.0, 0.5) == 0.0
    def test_full_vs_half(self):
        assert abs(kelly_fraction(0.60,2.0,1.0) - 2*kelly_fraction(0.60,2.0,0.5)) < 1e-4
    def test_zero_rr_zero(self):
        assert kelly_fraction(0.70, 0.0) == 0.0
    def test_known_value(self):
        # p=0.70, b=3.0 → f*=(0.70*3-0.30)/3=0.60; half=0.30
        assert abs(kelly_fraction(0.70, 3.0, 0.5) - 0.30) < 0.001

class TestPositionSizing:
    def test_basic(self):
        qty = position_size_from_kelly(10000, 0.02, 100, 98)
        assert abs(qty - 100.0) < 0.001
    def test_zero_stop_zero(self):
        assert position_size_from_kelly(10000, 0.02, 100, 100) == 0.0
    def test_max_cap(self):
        qty = position_size_from_kelly(10000, 1.0, 100, 99, max_position_pct=0.10)
        assert qty <= 10.0 + 1e-6
    def test_drawdown_scalar(self):
        q1 = position_size_from_kelly(10000, 0.02, 100, 98, drawdown_scalar=1.0)
        q5 = position_size_from_kelly(10000, 0.02, 100, 98, drawdown_scalar=0.5)
        assert abs(q5/q1 - 0.5) < 0.01
    def test_2pct_risk(self):
        qty = position_size_from_kelly(50000, 0.02, 200, 196)
        risk_usd = qty * 4
        assert abs(risk_usd/50000 - 0.02) < 0.001

class TestRiskParity:
    def test_equal_vol_equal_weights(self):
        w = risk_parity_weights({"a":0.20,"b":0.20,"c":0.20})
        for v in w.values(): assert abs(v - 1/3) < 0.001
    def test_sums_to_one(self):
        import random; random.seed(7)
        for _ in range(10):
            vols = {f"s{i}": random.uniform(0.05,0.80) for i in range(random.randint(2,6))}
            assert abs(sum(risk_parity_weights(vols).values()) - 1.0) < 1e-6
    def test_low_vol_higher_weight(self):
        w = risk_parity_weights({"low":0.10,"high":0.40})
        assert w["low"] > w["high"]
        assert abs(w["low"] - 0.80) < 0.001
    def test_risk_contribution_equal(self):
        vols = {"a":0.15,"b":0.30,"c":0.10}
        w = risk_parity_weights(vols)
        rc = [w[k]*v for k,v in vols.items()]
        assert max(rc)-min(rc) < 0.001
    def test_empty(self):
        assert risk_parity_weights({}) == {}

class TestCapitalAllocator:
    def test_pipeline(self):
        alloc = CapitalAllocator(100_000, kelly_fraction_=0.5, max_single_pos_pct=0.10)
        alloc.update_strategy_volatility("trend", 0.20)
        alloc.update_strategy_volatility("arb", 0.05)
        r = alloc.compute_position_size("trend", 0.62, 1.8, 50000, 49000)
        assert r["quantity"] > 0
        assert r["risk_usd"] == pytest.approx(r["quantity"]*1000, rel=0.01)
        assert "kelly_fraction" in r and "strategy_alloc" in r
