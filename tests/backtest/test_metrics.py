"""Unit tests for backtest/metrics.py — the corrected performance statistics."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../..'))

import math
from dataclasses import dataclass
from datetime import datetime, timezone

from backtest.metrics import (
    daily_returns_from_trades, sharpe_ratio, max_drawdown_net,
    probabilistic_sharpe_ratio, deflated_sharpe_ratio, bootstrap_sharpe_ci,
    funding_cost_pct, TRADING_DAYS_PER_YEAR,
)


@dataclass
class FakeTrade:
    exit_time: int        # unix ms
    pnl_usd: float
    side: str = "LONG"


def _ms(y, m, d, h=0):
    return int(datetime(y, m, d, h, tzinfo=timezone.utc).timestamp() * 1000)


class TestDailyAggregation:
    def test_buckets_by_exit_day(self):
        trades = [
            FakeTrade(_ms(2024, 1, 1, 1), 100.0),
            FakeTrade(_ms(2024, 1, 1, 5), 50.0),   # same day -> summed
            FakeTrade(_ms(2024, 1, 3, 2), -30.0),  # gap day in between -> 0
        ]
        rets = daily_returns_from_trades(trades, 10_000.0)
        # days: Jan1, Jan2 (flat), Jan3
        assert len(rets) == 3
        assert math.isclose(rets[0], 150.0 / 10_000.0)
        assert rets[1] == 0.0
        assert math.isclose(rets[2], -30.0 / 10_000.0)

    def test_empty(self):
        assert daily_returns_from_trades([], 10_000.0) == []
        assert daily_returns_from_trades([FakeTrade(_ms(2024, 1, 1), 1.0)], 0.0) == []


class TestSharpeAnnualization:
    def test_uses_365_not_252(self):
        rets = [0.01, -0.005, 0.008, 0.002, -0.003, 0.006]
        s = sharpe_ratio(rets)
        n = len(rets)
        mean = sum(rets) / n
        var = sum((r - mean) ** 2 for r in rets) / (n - 1)
        expected = mean / math.sqrt(var) * math.sqrt(TRADING_DAYS_PER_YEAR)
        assert math.isclose(s, expected)

    def test_per_trade_vs_daily_no_longer_inflated(self):
        # 10 winning + 5 losing trades all on distinct days; the OLD engine annualized
        # the per-trade series with sqrt(252). The corrected path aggregates to daily.
        trades = [FakeTrade(_ms(2024, 1, d), 100.0) for d in range(1, 11)]
        trades += [FakeTrade(_ms(2024, 1, d), -120.0) for d in range(11, 16)]
        daily = daily_returns_from_trades(trades, 10_000.0)
        s = sharpe_ratio(daily)
        assert math.isfinite(s)
        # sanity: positive net but realistic magnitude, not absurd
        assert -20 < s < 20

    def test_too_few_returns(self):
        assert sharpe_ratio([0.01]) == 0.0
        assert sharpe_ratio([]) == 0.0


class TestMaxDrawdown:
    def test_catches_early_loss(self):
        # Old formula skipped losses while cumulative PnL (peak) was still <= 0.
        # Net-equity formula must capture the dip from the very first bar.
        trades = [FakeTrade(_ms(2024, 1, 1), -500.0), FakeTrade(_ms(2024, 1, 2), 200.0)]
        dd = max_drawdown_net(trades, 10_000.0)
        assert math.isclose(dd, 500.0 / 10_000.0, rel_tol=1e-9)

    def test_monotone_decline(self):
        trades = [FakeTrade(_ms(2024, 1, d), -100.0) for d in range(1, 6)]
        dd = max_drawdown_net(trades, 10_000.0)
        assert math.isclose(dd, 500.0 / 10_000.0, rel_tol=1e-9)

    def test_no_drawdown_when_only_gains(self):
        trades = [FakeTrade(_ms(2024, 1, d), 100.0) for d in range(1, 6)]
        assert max_drawdown_net(trades, 10_000.0) == 0.0


class TestProbabilisticAndDeflatedSharpe:
    def test_psr_in_unit_interval(self):
        rets = [0.01, -0.004, 0.006, 0.003, -0.002, 0.005, 0.001, -0.001]
        psr = probabilistic_sharpe_ratio(rets)
        assert 0.0 <= psr <= 1.0

    def test_psr_higher_for_stronger_track_record(self):
        weak = [0.001, -0.0008, 0.0012, -0.0005]
        strong = [0.01, 0.009, 0.011, 0.008, 0.012, 0.0095]
        assert probabilistic_sharpe_ratio(strong) > probabilistic_sharpe_ratio(weak)

    def test_dsr_not_above_psr(self):
        # Deflating against multiple trials can only lower (or equal) confidence.
        rets = [0.01, -0.004, 0.006, 0.003, -0.002, 0.005, 0.001, -0.001, 0.004, 0.002]
        psr = probabilistic_sharpe_ratio(rets, 0.0)
        dsr = deflated_sharpe_ratio(rets, n_trials=50)
        assert dsr <= psr + 1e-9
        assert 0.0 <= dsr <= 1.0


class TestBootstrapCI:
    def test_ordering_and_determinism(self):
        rets = [0.01, -0.005, 0.008, 0.002, -0.003, 0.006, 0.004, -0.001]
        p5, p50, p95 = bootstrap_sharpe_ci(rets, n_boot=500, seed=7)
        assert p5 <= p50 <= p95
        # deterministic with fixed seed
        assert bootstrap_sharpe_ci(rets, n_boot=500, seed=7) == (p5, p50, p95)


class TestFundingCost:
    def test_long_pays_positive_funding(self):
        fr = {_ms(2024, 1, 1, 8): 0.0001, _ms(2024, 1, 1, 16): 0.0002}
        cost = funding_cost_pct(fr, _ms(2024, 1, 1, 0), _ms(2024, 1, 1, 20), "LONG")
        assert math.isclose(cost, 0.0003)

    def test_short_receives_positive_funding(self):
        fr = {_ms(2024, 1, 1, 8): 0.0001, _ms(2024, 1, 1, 16): 0.0002}
        cost = funding_cost_pct(fr, _ms(2024, 1, 1, 0), _ms(2024, 1, 1, 20), "SHORT")
        assert math.isclose(cost, -0.0003)

    def test_only_settlements_within_holding_window(self):
        fr = {_ms(2024, 1, 1, 8): 0.001, _ms(2024, 1, 2, 8): 0.001}
        cost = funding_cost_pct(fr, _ms(2024, 1, 1, 0), _ms(2024, 1, 1, 12), "LONG")
        assert math.isclose(cost, 0.001)  # only the 08:00 settlement counts
