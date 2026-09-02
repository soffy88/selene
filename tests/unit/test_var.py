"""Unit tests for VaR engine and Drawdown Controller."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
from services.risk.portfolio.var_engine import DrawdownController, calc_historical_var


class TestHistoricalVaR:
    def test_returns_none_on_insufficient_data(self):
        assert calc_historical_var([1.0] * 10) is None

    def test_var_positive(self):
        import random

        random.seed(42)
        returns = [random.gauss(10, 100) for _ in range(252)]
        r = calc_historical_var(returns)
        assert r is not None
        assert r.var_95 >= 0
        assert r.var_99 >= r.var_95  # 99% VaR >= 95% VaR

    def test_cvar_gte_var(self):
        import random

        random.seed(99)
        returns = [random.gauss(0, 50) for _ in range(252)]
        r = calc_historical_var(returns)
        assert r.expected_shortfall >= r.var_95

    def test_backtest_coverage(self):
        """Fraction of days exceeding VaR should ≈ 5% (allow ±3%)."""
        import random

        random.seed(1)
        returns = [random.gauss(0, 100) for _ in range(1000)]
        r = calc_historical_var(returns, confidence=0.95)
        exceedances = sum(1 for x in returns if x < -r.var_95)
        breach_rate = exceedances / len(returns)
        assert 0.02 <= breach_rate <= 0.08  # within ±3% of 5%

    def test_fat_tail_var_higher_than_normal(self):
        """Fat-tailed distribution should produce higher VaR than normal."""
        import random

        random.seed(5)
        normal = [random.gauss(0, 100) for _ in range(500)]
        fat_tail = [
            random.gauss(0, 100) + (random.choice([-1, 1]) * 500 if random.random() < 0.05 else 0) for _ in range(500)
        ]
        r_norm = calc_historical_var(normal)
        r_fat = calc_historical_var(fat_tail)
        assert r_fat.var_99 > r_norm.var_99  # fat tails hit harder at 99%


class TestDrawdownController:
    def test_initial_level_green(self):
        dc = DrawdownController()
        level = dc.update(10000)
        assert level.name == "GREEN"

    def test_yellow_threshold(self):
        dc = DrawdownController()
        dc.update(10000)  # peak = 10000
        level = dc.update(9400)  # DD = 6% → YELLOW
        assert level.name == "YELLOW"
        assert level.max_position_pct == 0.50

    def test_orange_threshold(self):
        dc = DrawdownController()
        dc.update(10000)
        level = dc.update(8900)  # DD = 11% → ORANGE
        assert level.name == "ORANGE"
        assert level.max_position_pct == 0.25

    def test_red_halt_triggered(self):
        dc = DrawdownController()
        dc.update(10000)
        level = dc.update(8400)  # DD = 16% → RED
        assert level.name == "RED"
        assert not level.new_trades_allowed
        assert dc.is_halted
        assert dc.position_scalar == 0.0

    def test_peak_tracks_correctly(self):
        dc = DrawdownController()
        dc.update(10000)
        dc.update(12000)  # new peak
        dc.update(11000)  # DD = (12000-11000)/12000 = 8.3% → YELLOW
        assert dc.current_dd == pytest.approx(1000 / 12000, rel=0.01)

    def test_manual_reset(self):
        dc = DrawdownController()
        dc.update(10000)
        dc.update(8000)  # RED
        assert dc.is_halted
        dc.manual_reset()
        assert not dc.is_halted

    def test_max_dd_tracks(self):
        dc = DrawdownController()
        dc.update(10000)
        dc.update(8000)
        dc.update(9000)  # partial recovery
        assert dc.max_dd == pytest.approx(0.20, rel=0.01)  # worst was 20%

    def test_get_status_structure(self):
        dc = DrawdownController()
        dc.update(10000)
        dc.update(9500)
        s = dc.get_status()
        for key in ["level", "current_dd_pct", "max_dd_pct", "position_scalar", "new_trades"]:
            assert key in s
