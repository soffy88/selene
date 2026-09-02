"""Liquidation-distance gate tests (audit P0-1).

The existential gate for a leveraged perps account: a position must (a) keep a
minimum buffer to its liquidation price and (b) carry a protective stop that
triggers BEFORE liquidation — otherwise a fast adverse move force-liquidates the
position before the stop can ever fill.
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


# ── check_liquidation_distance (unit) ──────────────────────────────────────


def test_sane_leverage_and_tight_stop_passes():
    # 1x position (10k notional on 10k equity), 2% stop, liq_dist ≈ 99.5% → fine.
    ok, reason = rm._gate.check_liquidation_distance(
        allocated_usd=10_000.0, entry_price=100.0, stop_price=98.0, equity=10_000.0
    )
    assert ok is True, reason


def test_stop_beyond_liquidation_rejected(monkeypatch):
    # 8x cross leverage: liq_dist ≈ 1/8 - 0.005 = 0.12. A 20% stop sits well beyond
    # liquidation → the position liquidates before the stop fills → reject.
    monkeypatch.setattr(rm, "_open_positions", {"BTCUSDT": {"side": "LONG", "notional": 70_000.0}}, raising=False)
    ok, reason = rm._gate.check_liquidation_distance(
        allocated_usd=10_000.0, entry_price=100.0, stop_price=80.0, equity=10_000.0
    )
    assert ok is False
    assert "liquidate first" in reason or "liq_dist" in reason


def test_excessive_leverage_backstop_rejected(monkeypatch):
    # 12x cross leverage: liq_dist ≈ 1/12 - 0.005 ≈ 0.078 ≤ MIN_LIQ_BUFFER_PCT (0.10)
    # → rejected purely on the leverage backstop, even with no stop info.
    monkeypatch.setattr(rm, "_open_positions", {"BTCUSDT": {"side": "LONG", "notional": 110_000.0}}, raising=False)
    ok, reason = rm._gate.check_liquidation_distance(
        allocated_usd=10_000.0, entry_price=0.0, stop_price=0.0, equity=10_000.0
    )
    assert ok is False
    assert "min buffer" in reason


def test_missing_prices_still_allows_low_leverage():
    # No price info, modest leverage → can't assert the stop check, backstop passes.
    ok, _ = rm._gate.check_liquidation_distance(
        allocated_usd=20_000.0, entry_price=0.0, stop_price=0.0, equity=10_000.0
    )
    assert ok is True


def test_zero_equity_or_alloc_is_noop():
    assert rm._gate.check_liquidation_distance(0.0, 100.0, 98.0, 10_000.0)[0] is True
    assert rm._gate.check_liquidation_distance(10_000.0, 100.0, 98.0, 0.0)[0] is True


# ── approve() integration ──────────────────────────────────────────────────


def test_approve_blocks_stop_beyond_liquidation(monkeypatch):
    _set_corr_returns(monkeypatch, {})
    monkeypatch.setattr(
        rm,
        "_open_positions",
        {"__equity__": {"value": 10_000.0}, "BTCUSDT": {"side": "LONG", "notional": 18_000.0}},
        raising=False,
    )
    # New 8k order → ~2.6x leverage (passes MAX_LEVERAGE 3x), but a 40% stop sits
    # beyond the ~38% liquidation distance → Gate 5b must reject.
    order = {
        "symbol": "ETHUSDT",
        "side": "BUY",
        "allocated_usd": 8_000.0,
        "win_probability": 0.99,
        "quantity": 1.0,
        "entry_price": 100.0,
        "stop_price": 60.0,
    }
    approved, reason, _ = asyncio.run(rm._gate.approve(order))
    assert approved is False
    assert "liq" in reason.lower() or "liquidat" in reason.lower()


def test_approve_allows_tight_stop(monkeypatch):
    _set_corr_returns(monkeypatch, {})
    monkeypatch.setattr(rm, "_open_positions", {"__equity__": {"value": 10_000.0}}, raising=False)
    order = {
        "symbol": "BTCUSDT",
        "side": "BUY",
        "allocated_usd": 5_000.0,
        "win_probability": 0.99,
        "quantity": 1.0,
        "entry_price": 100.0,
        "stop_price": 98.0,
    }
    approved, reason, _ = asyncio.run(rm._gate.approve(order))
    assert approved is True, reason
