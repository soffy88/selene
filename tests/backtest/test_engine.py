"""Tests for the WFO backtest engine (optimization item #3).

The engine previously had zero tests. These cover the three behaviours the
item fixed: (1) WFO performs a real in-sample parameter search and records the
winner per window, (2) the DSR trial count == grid size, and (3) the OOS window
starts after the train+embargo offset. A deterministic injected signal_fn keeps
trade generation reproducible (no RNG).
"""

from __future__ import annotations

import asyncio
import math

from backtest.engine import WFOConfig, WFOEngine, default_param_grid

HOUR_MS = 3_600_000
BASE_MS = 1_700_000_000_000


def _make_candles(n: int) -> list[dict]:
    """Deterministic price series with enough swing to move indicators."""
    candles = []
    for i in range(n):
        close = 30000 + 2000 * math.sin(i / 10.0) + 600 * math.sin(i / 3.0)
        high = close * 1.004
        low = close * 0.996
        candles.append(
            {
                "open_time": BASE_MS + i * HOUR_MS,
                "high": high,
                "low": low,
                "close": close,
            }
        )
    return candles


def _make_funding(n: int) -> list[dict]:
    # Alternate strong negative / positive funding so both long/short rules can fire.
    out = []
    for i in range(n):
        fr = -0.10 if (i // 12) % 2 == 0 else 0.15
        out.append({"funding_time": BASE_MS + i * HOUR_MS, "funding_rate": fr})
    return out


def _small_config(**kw) -> WFOConfig:
    base = dict(train_days=3, test_days=1, step_days=1, embargo_days=1, min_oos_trades=1)
    base.update(kw)
    return WFOConfig(**base)


def test_run_records_trials_and_per_window_params():
    cfg = _small_config()
    engine = WFOEngine(cfg)
    candles = _make_candles(300)
    result = asyncio.run(engine.run("BTC-USDT", candles, _make_funding(300)))

    # DSR trial count is the grid size, not the window count.
    assert result.n_trials == len(cfg.param_grid)
    assert result.n_trials == len(default_param_grid())

    # One selected-params record per evaluated window, each a grid member.
    train_bars = cfg.train_days * 24
    test_bars = cfg.test_days * 24
    embargo_bars = cfg.embargo_days * 24
    step_bars = cfg.step_days * 24
    n_windows = 0
    start = 0
    while start + train_bars + embargo_bars + test_bars <= len(candles):
        n_windows += 1
        start += step_bars
    assert len(result.selected_params) == n_windows
    for p in result.selected_params:
        assert p in cfg.param_grid


def test_oos_period_starts_after_train_plus_embargo():
    cfg = _small_config()

    # Deterministic signal: enter LONG on a fixed cadence so window 0 has trades.
    def sig(closes, highs, lows, price, fr, regime, params):
        return [{"type": "X", "side": "LONG"}] if len(closes) % 5 == 0 else []

    engine = WFOEngine(cfg, signal_fn=sig)
    candles = _make_candles(300)
    result = asyncio.run(engine.run("BTC-USDT", candles, _make_funding(300)))

    assert result.periods, "expected at least one OOS period with trades"
    train_bars = cfg.train_days * 24
    embargo_bars = cfg.embargo_days * 24
    expected_test_start = train_bars + embargo_bars
    assert result.periods[0].period_start == candles[expected_test_start]["open_time"]
    assert all(p.is_oos for p in result.periods)


def test_oos_metrics_and_to_dict_wired():
    cfg = _small_config()

    def sig(closes, highs, lows, price, fr, regime, params):
        return [{"type": "X", "side": "LONG"}] if len(closes) % 5 == 0 else []

    engine = WFOEngine(cfg, signal_fn=sig)
    candles = _make_candles(400)
    result = asyncio.run(engine.run("BTC-USDT", candles, _make_funding(400)))

    assert result.oos_n_trades > 0
    assert math.isfinite(result.oos_sharpe)
    d = result.to_dict()
    assert d["n_trials"] == len(cfg.param_grid)
    assert "selected_params" in d


def test_optimize_params_falls_back_when_no_trades():
    cfg = _small_config()
    engine = WFOEngine(cfg, signal_fn=lambda *a, **k: [])  # never trades
    grid = cfg.param_grid
    train = {
        "closes": [100.0] * 100,
        "highs": [101.0] * 100,
        "lows": [99.0] * 100,
        "times": [BASE_MS + i * HOUR_MS for i in range(100)],
    }
    best = engine._optimize_params("BTC-USDT", train, [], [], [], {}, grid)
    assert best is grid[0]


def test_insufficient_data_returns_empty():
    engine = WFOEngine(_small_config())
    result = asyncio.run(engine.run("BTC-USDT", _make_candles(50), _make_funding(50)))
    assert result.oos_n_trades == 0
    assert result.periods == []
