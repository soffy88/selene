"""VaR-gate tests (optimization item #9).

The enforced VaR gate now uses correlation-aware post-trade VaR (w'Σw) as the
primary path and a scaled equity-series historical VaR as fallback, replacing
the arbitrary `var_pct + allocated/equity*0.15` estimate.
"""

import asyncio

import pytest

import services.risk.main as rm


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    monkeypatch.setattr(rm, "_open_positions", {}, raising=False)
    monkeypatch.setattr(rm, "_equity_history", [], raising=False)
    yield


def _set_corr_returns(monkeypatch, data):
    async def fake(symbols):
        return data

    monkeypatch.setattr(rm, "_get_corr_returns", fake)


def test_correlated_path_rejects_large_var(monkeypatch):
    # Volatile, perfectly correlated series → large w'Σw for a big position.
    rets = [0.05, -0.05, 0.05, -0.05, 0.05, -0.05, 0.05, -0.05]
    _set_corr_returns(monkeypatch, {"BTCUSDT": rets})
    ok, reason = asyncio.run(rm._gate.check_var("BTCUSDT", "LONG", 50_000.0, 10_000.0))
    assert ok is False
    assert "VaR" in reason and "correlated" in reason


def test_correlated_path_allows_small_var(monkeypatch):
    rets = [0.001, -0.001, 0.001, -0.001, 0.001, -0.001]
    _set_corr_returns(monkeypatch, {"BTCUSDT": rets})
    ok, reason = asyncio.run(rm._gate.check_var("BTCUSDT", "LONG", 100.0, 10_000.0))
    assert ok is True


def test_fallback_used_when_no_corr_returns(monkeypatch):
    _set_corr_returns(monkeypatch, {})  # no per-symbol data
    # Build an equity history with ~2% per-bar swings.
    hist = []
    eq = 10_000.0
    for i in range(40):
        eq *= 1.02 if i % 2 == 0 else 0.98
        hist.append(eq)
    monkeypatch.setattr(rm, "_equity_history", hist, raising=False)
    # Tiny allocation passes; an enormous one scales VaR past the limit.
    ok_small, _ = asyncio.run(rm._gate.check_var("BTCUSDT", "LONG", 10.0, 10_000.0))
    ok_big, reason_big = asyncio.run(rm._gate.check_var("BTCUSDT", "LONG", 500_000.0, 10_000.0))
    assert ok_small is True
    assert ok_big is False
    assert "VaR" in reason_big


def test_skips_when_no_data(monkeypatch):
    _set_corr_returns(monkeypatch, {})
    # No equity history → insufficient data → allow (don't block on nothing).
    ok, reason = asyncio.run(rm._gate.check_var("BTCUSDT", "LONG", 1000.0, 10_000.0))
    assert ok is True
