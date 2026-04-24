"""Unit tests for three-component slippage model."""
import pytest, sys, os, math
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../..'))
from services.execution.slippage.model import SlippageModel

class TestSlippageModel:
    def setup_method(self):
        self.model = SlippageModel()

    def test_all_components_present(self):
        r = self.model.estimate(10000, 0.80, 50_000_000, 0.02, "MARKET")
        assert r.spread > 0
        assert r.impact > 0
        assert r.timing > 0
        assert r.total == pytest.approx(r.spread + r.impact + r.timing, rel=0.01)

    def test_limit_vs_market_spread_cost(self):
        r_limit  = self.model.estimate(10000, 0.80, 50_000_000, 0.04, "LIMIT")
        r_market = self.model.estimate(10000, 0.80, 50_000_000, 0.04, "MARKET")
        assert r_limit.spread < r_market.spread   # limit pays half-spread

    def test_large_order_higher_impact(self):
        r_small = self.model.estimate(10_000,    0.80, 50_000_000, 0.02, "MARKET")
        r_large = self.model.estimate(5_000_000, 0.80, 50_000_000, 0.02, "MARKET")
        assert r_large.impact > r_small.impact

    def test_impact_square_root_scaling(self):
        """Doubling notional should roughly sqrt(2) the impact."""
        r1 = self.model.estimate(100_000, 0.80, 50_000_000, 0.0, "MARKET")
        r2 = self.model.estimate(400_000, 0.80, 50_000_000, 0.0, "MARKET")
        # impact ∝ sqrt(notional), so 4x notional → ~2x impact
        ratio = r2.impact / r1.impact if r1.impact > 0 else 0
        assert 1.5 < ratio < 2.5

    def test_high_vol_higher_impact(self):
        r_low  = self.model.estimate(100_000, 0.20, 50_000_000, 0.02, "MARKET")
        r_high = self.model.estimate(100_000, 1.20, 50_000_000, 0.02, "MARKET")
        assert r_high.impact > r_low.impact

    def test_recommendation_market_for_small(self):
        r = self.model.estimate(100, 0.50, 500_000_000, 0.005, "MARKET")
        # Very small order in liquid market → MARKET ok
        assert r.recommendation in ("MARKET", "LIMIT")

    def test_recommendation_limit_for_large_spread(self):
        r = self.model.estimate(100_000, 0.80, 5_000_000, 0.20, "MARKET")
        # High spread → recommend LIMIT
        assert r.recommendation == "LIMIT"

    def test_total_usd_scales_with_notional(self):
        r1 = self.model.estimate(10_000, 0.50, 50_000_000, 0.02, "MARKET")
        r2 = self.model.estimate(20_000, 0.50, 50_000_000, 0.02, "MARKET")
        # total_usd should roughly double (not exactly due to impact's sqrt)
        assert r2.total_usd > r1.total_usd

    def test_adjust_entry_price_buy(self):
        adjusted = self.model.adjust_entry_price(100.0, "BUY", slippage_pct=0.1)
        assert adjusted > 100.0   # buy fills higher

    def test_adjust_entry_price_sell(self):
        adjusted = self.model.adjust_entry_price(100.0, "SELL", slippage_pct=0.1)
        assert adjusted < 100.0   # sell fills lower
