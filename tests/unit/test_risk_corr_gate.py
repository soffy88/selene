"""Phase 5 wiring — dynamic correlation gate in the running risk service.

Exercises RiskGate.check_corr_exposure_dynamic with the DB fetch and equity mocked, so the
real wiring (position registry -> returns -> correlated exposure -> reject/allow) is verified.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import pytest

import services.risk.main as risk

# A,B strongly correlated; C uncorrelated/anti.
_RETURNS = {
    "BTCUSDT": [0.01, -0.02, 0.03, -0.01, 0.02, 0.01, -0.015],
    "ETHUSDT": [0.011, -0.019, 0.031, -0.009, 0.021, 0.012, -0.014],
    "XYZUSDT": [-0.01, 0.02, -0.03, 0.01, -0.02, -0.01, 0.015],
}


@pytest.fixture(autouse=True)
def _patch(monkeypatch):
    async def fake_returns(symbols):
        return {s: _RETURNS[s] for s in symbols if s in _RETURNS}

    monkeypatch.setattr(risk, "_get_corr_returns", fake_returns)
    monkeypatch.setattr(risk, "_get_equity", lambda: 100_000.0)
    monkeypatch.setattr(risk, "MAX_CORR_EXPOSURE", 0.30)
    risk._open_positions.clear()
    yield
    risk._open_positions.clear()


async def test_rejects_when_correlated_same_dir_exceeds_cap():
    # Existing long ETH 25k; candidate long BTC 10k. ETH~BTC correlated -> 35k/100k = 35% > 30%.
    risk._open_positions["ETHUSDT"] = {"side": "LONG", "notional": 25_000.0}
    gate = risk.RiskGate()
    ok, reason = await gate.check_corr_exposure_dynamic("BTCUSDT", "BUY", 10_000.0)
    assert not ok and "corr_exposure(dynamic)" in reason


async def test_allows_when_uncorrelated():
    # Existing long XYZ 25k (uncorrelated with BTC); candidate long BTC 10k -> only 10k counts.
    risk._open_positions["XYZUSDT"] = {"side": "LONG", "notional": 25_000.0}
    gate = risk.RiskGate()
    ok, reason = await gate.check_corr_exposure_dynamic("BTCUSDT", "BUY", 10_000.0)
    assert ok and reason == ""


async def test_allows_when_opposite_direction():
    # ETH short, candidate BTC long -> not same direction -> 10k only.
    risk._open_positions["ETHUSDT"] = {"side": "SHORT", "notional": 25_000.0}
    gate = risk.RiskGate()
    ok, _ = await gate.check_corr_exposure_dynamic("BTCUSDT", "BUY", 10_000.0)
    assert ok


async def test_falls_back_to_static_when_no_return_data(monkeypatch):
    async def empty(symbols):
        return {}

    monkeypatch.setattr(risk, "_get_corr_returns", empty)
    risk._open_positions["ETHUSDT"] = {"side": "LONG", "notional": 25_000.0}
    gate = risk.RiskGate()
    # static fallback uses CORR_GROUPS (BTC & ETH share a group): 25k/100k = 25% < 30% -> allowed
    ok, _ = await gate.check_corr_exposure_dynamic("BTCUSDT", "BUY", 10_000.0)
    assert ok
