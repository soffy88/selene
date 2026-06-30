"""Tests for backtest.signal_edge — the predictive-power measurement the system
lacked. The evaluator must DETECT a planted edge, REJECT pure noise, and CATCH an
anti-signal; then it is wired to the deployed engine's real per-bar signal."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import math
import numpy as np
import pandas as pd
import pytest

from backtest.signal_edge import (
    forward_returns,
    evaluate_signal_edge,
    engine_cusum_signal_edge,
)


def test_forward_returns_basic():
    closes = [100.0, 110.0, 121.0]
    fwd = forward_returns(closes, horizon=1)
    assert fwd[0] == pytest.approx(0.10)
    assert fwd[1] == pytest.approx(0.10)
    assert math.isnan(fwd[2])     # no future bar for the last close


def _prices_from_returns(rets):
    return 100.0 * np.exp(np.cumsum(rets))


def test_detects_planted_edge():
    """A signal that is forward return + small noise must show strong positive IC
    and an above-coin-flip hit-rate."""
    rng = np.random.default_rng(0)
    n = 600
    rets = rng.normal(0, 0.01, n)
    closes = _prices_from_returns(rets)
    fwd = forward_returns(closes, 1)
    # signal leads the forward return (with noise); align lengths
    signal = np.where(np.isnan(fwd), 0.0, fwd) + rng.normal(0, 0.002, n)

    edge = evaluate_signal_edge(signal, closes, horizon=1)
    assert edge.ic > 0.5
    assert edge.hit_rate > 0.6
    assert edge.n > 100


def test_rejects_pure_noise():
    """An independent-noise signal must show near-zero IC and ~50% hit-rate."""
    rng = np.random.default_rng(1)
    n = 800
    closes = _prices_from_returns(rng.normal(0, 0.01, n))
    signal = rng.normal(0, 1.0, n)      # unrelated to price

    edge = evaluate_signal_edge(signal, closes, horizon=1)
    assert abs(edge.ic) < 0.15
    assert 0.4 < edge.hit_rate < 0.6


def test_catches_anti_signal():
    """A signal equal to the NEGATIVE forward return must show strong negative IC."""
    rng = np.random.default_rng(2)
    n = 500
    closes = _prices_from_returns(rng.normal(0, 0.01, n))
    fwd = forward_returns(closes, 1)
    signal = np.where(np.isnan(fwd), 0.0, -fwd)

    edge = evaluate_signal_edge(signal, closes, horizon=1)
    assert edge.ic < -0.5
    assert edge.hit_rate < 0.4


def test_engine_signal_hook_is_wellformed():
    """The real-engine hook returns a well-formed measurement on real bars
    (IC bounded, hit-rate a probability) — a usable handle on live signal edge."""
    rng = np.random.default_rng(7)
    n = 300
    t0 = datetime(2024, 1, 1, tzinfo=timezone.utc)
    closes = _prices_from_returns(rng.normal(0, 0.01, n))
    df = pd.DataFrame({
        "time": pd.to_datetime([t0 + timedelta(hours=4 * i) for i in range(n)]),
        "close": closes, "open": closes, "high": closes * 1.002,
        "low": closes * 0.998, "volume": 1000.0,
    })
    edge = engine_cusum_signal_edge(df, horizon=1)
    assert -1.0 <= edge.ic <= 1.0
    assert 0.0 <= edge.hit_rate <= 1.0
    assert edge.n > 50
