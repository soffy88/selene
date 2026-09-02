"""
Unit tests for VaR Engine and DrawdownController.
Key: VaR back-test check — actual exceedance rate should ≈ 5%.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
import random

from services.risk.portfolio.var_engine import (
    DRAWDOWN_LEVELS,
    DrawdownController,
    calc_historical_var,
)


class TestHistoricalVaR:
    def _fat_tail_returns(self, n=500, seed=42):
        """Simulate crypto-like fat-tail returns (not normal)."""
        random.seed(seed)
        returns = []
        for _ in range(n):
            if random.random() < 0.05:
                returns.append(random.uniform(-500, -100))  # fat tail
            else:
                returns.append(random.gauss(10, 80))
        return returns

    def test_basic_var_calculation(self):
        returns = self._fat_tail_returns()
        result = calc_historical_var(returns, confidence=0.95)
        assert result is not None
        assert result.var_95 > 0
        assert result.var_99 >= result.var_95
        assert result.expected_shortfall >= result.var_95
        assert result.n_observations == len(returns)

    def test_cvar_exceeds_var(self):
        """CVaR (expected shortfall) must always be ≥ VaR."""
        returns = self._fat_tail_returns(1000)
        r = calc_historical_var(returns)
        assert r.expected_shortfall >= r.var_95

    def test_back_test_exceedance_rate(self):
        """
        Key validation: actual exceedance rate should ≈ 5% (±2%).
        If VaR is computed correctly, about 5% of days should exceed it.
        """
        random.seed(99)
        returns = [random.gauss(0, 100) for _ in range(500)]
        r = calc_historical_var(returns, 0.95)
        actual_exceed = sum(1 for ret in returns if ret < -r.var_95)
        exceedance_rate = actual_exceed / len(returns)
        assert abs(exceedance_rate - 0.05) < 0.03, f"Expected ~5% exceedance, got {exceedance_rate:.1%}"

    def test_insufficient_data_returns_none(self):
        assert calc_historical_var([1.0] * 20) is None
        assert calc_historical_var([]) is None

    def test_higher_confidence_gives_higher_var(self):
        returns = self._fat_tail_returns()
        r95 = calc_historical_var(returns, 0.95)
        r99 = calc_historical_var(returns, 0.99)
        assert r99.var_99 >= r95.var_95

    def test_all_gains_gives_zero_var(self):
        """If all returns are positive, VaR should be 0 (no loss)."""
        returns = [abs(random.gauss(50, 10)) for _ in range(100)]
        r = calc_historical_var(returns)
        assert r is not None
        assert r.var_95 == 0.0


class TestDrawdownController:
    def test_green_at_start(self):
        dc = DrawdownController()
        level = dc.update(10000)
        assert level.name == "GREEN"
        assert dc.position_scalar == 1.0
        assert not dc.is_halted

    def test_yellow_at_7pct_drawdown(self):
        dc = DrawdownController()
        dc.update(10000)
        level = dc.update(9250)  # 7.5% drawdown
        assert level.name == "YELLOW"
        assert dc.position_scalar == 0.50

    def test_orange_at_12pct_drawdown(self):
        dc = DrawdownController()
        dc.update(10000)
        level = dc.update(8750)  # 12.5% drawdown
        assert level.name == "ORANGE"
        assert dc.position_scalar == 0.25

    def test_red_halt_at_15pct(self):
        dc = DrawdownController()
        dc.update(10000)
        level = dc.update(8400)  # 16% drawdown
        assert level.name == "RED"
        assert dc.position_scalar == 0.00
        assert dc.is_halted

    def test_peak_tracks_correctly(self):
        dc = DrawdownController()
        dc.update(10000)
        dc.update(12000)  # new peak
        dc.update(11000)  # drawdown from 12k
        assert abs(dc.current_dd - 1 / 12) < 0.001

    def test_max_drawdown_persists(self):
        """max_dd should not decrease even if equity recovers."""
        dc = DrawdownController()
        dc.update(10000)
        dc.update(8500)  # 15% dd
        dc.update(10000)  # recovery
        assert dc.max_dd >= 0.149  # still remembers the worst

    def test_manual_reset_clears_halt(self):
        dc = DrawdownController()
        dc.update(10000)
        dc.update(8000)  # 20% → RED
        assert dc.is_halted
        dc.manual_reset()
        assert not dc.is_halted

    def test_status_structure(self):
        dc = DrawdownController()
        dc.update(10000)
        s = dc.get_status()
        assert "level" in s
        assert "current_dd_pct" in s
        assert "max_dd_pct" in s
        assert "position_scalar" in s
        assert "new_trades" in s

    def test_levels_are_ordered(self):
        """Drawdown levels must be ordered by threshold."""
        for i in range(len(DRAWDOWN_LEVELS) - 1):
            assert DRAWDOWN_LEVELS[i].threshold_hi == DRAWDOWN_LEVELS[i + 1].threshold_lo

    def test_position_scalar_decreases_with_severity(self):
        """More severe drawdown → smaller position scalar."""
        scalars = [lvl.max_position_pct for lvl in DRAWDOWN_LEVELS]
        for i in range(len(scalars) - 1):
            assert scalars[i] >= scalars[i + 1]
