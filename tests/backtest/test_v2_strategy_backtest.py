"""Tests for backtest.v2_strategy_backtest — the harness that finally evaluates the
REAL deployed sel_v2 strategies (not the built-in WFO toy rules)."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import math
import numpy as np
import pandas as pd

from backtest.v2_strategy_backtest import (
    run_v2_backtest,
    evaluate_trades,
    check_pass,
    _TradeView,
)


def _scenario(n: int = 560):
    """Bar frame that drives real Strategy-1 entries and exits (same shape as the
    paper-engine integration scenario)."""
    rng = np.random.default_rng(11)
    t0 = datetime(2024, 1, 1, tzinfo=timezone.utc)
    times = [t0 + timedelta(hours=4 * i) for i in range(n)]
    rets = rng.normal(0, 0.01, n)
    rets[470:500] += 0.009
    rets[505:515] -= 0.04
    close = 30000 * np.exp(np.cumsum(rets))
    df = pd.DataFrame({"time": pd.to_datetime(times), "close": close, "open": close,
                       "high": close * 1.003, "low": close * 0.997, "volume": 1000.0})
    funding = np.full(n, 0.0008)
    return df, funding


def test_harness_evaluates_real_strategies_end_to_end():
    df, funding = _scenario()
    res = run_v2_backtest(df, initial_capital=100_000, funding_series=funding,
                          skip_hawkes=True, skip_tda=True, hawkes_params=(0.1, 0.3, 0.5))

    assert "all" in res and "by_strategy" in res and "engine_summary" in res
    m = res["all"]
    # the deployed strategies actually traded and were measured
    assert m["n_trades"] > 0
    assert 0.0 <= m["win_rate"] <= 1.0
    for key in ("sharpe", "psr", "dsr", "max_drawdown", "return_pct"):
        assert math.isfinite(m[key])
    assert 0.0 <= m["dsr"] <= 1.0
    assert 0.0 <= m["max_drawdown"] <= 1.0
    # per-strategy trade counts partition the total
    s1 = res["by_strategy"]["strategy_1"]["n_trades"]
    s2 = res["by_strategy"]["strategy_2"]["n_trades"]
    assert s1 + s2 == m["n_trades"]


def test_oos_slice_is_subset_and_gated():
    df, funding = _scenario()
    # cut OOS at the midpoint of the bar timeline
    mid = df["time"].iloc[len(df) // 2]
    oos_ms = int(mid.timestamp() * 1000)
    res = run_v2_backtest(df, initial_capital=100_000, funding_series=funding,
                          skip_hawkes=True, skip_tda=True, hawkes_params=(0.1, 0.3, 0.5),
                          oos_start_ms=oos_ms)
    assert "oos" in res and "oos_passed" in res
    assert res["oos"]["n_trades"] <= res["all"]["n_trades"]
    # the gate is a real boolean with reasons when it fails
    assert isinstance(res["oos_passed"], bool)
    if not res["oos_passed"]:
        assert res["oos_fail_reasons"]


def test_empty_trades_safe():
    """A flat market produces no trades; metrics must be well-defined zeros."""
    n = 120
    t0 = datetime(2024, 1, 1, tzinfo=timezone.utc)
    df = pd.DataFrame({
        "time": pd.to_datetime([t0 + timedelta(hours=4 * i) for i in range(n)]),
        "close": np.full(n, 30000.0), "open": np.full(n, 30000.0),
        "high": np.full(n, 30000.0), "low": np.full(n, 30000.0), "volume": 1000.0,
    })
    res = run_v2_backtest(df, skip_hawkes=True, skip_tda=True, hawkes_params=(0.1, 0.3, 0.5))
    assert res["all"]["n_trades"] == 0
    assert res["all"]["sharpe"] == 0.0
    assert res["all"]["max_drawdown"] == 0.0


def test_check_pass_flags_weak_metrics():
    weak = evaluate_trades([
        _TradeView(exit_time=1000 + i, pnl_usd=-10.0, strategy="strategy_1",
                   direction="LONG", entry_time_ms=0)
        for i in range(5)
    ], initial_capital=100_000)
    passed, reasons = check_pass(weak)
    assert passed is False
    # too few trades, sub-floor win-rate and Sharpe should all be cited
    assert any("n_trades" in r for r in reasons)
    assert any("win_rate" in r for r in reasons)
