"""PortfolioState risk-field population (optimization item #17)."""
import asyncio

from services.portfolio.main import PortfolioEngine
from shared.models.portfolio import Position, PositionSide


def _open(engine, symbol, entry, qty, strat="strategy_1"):
    pos = Position(symbol=symbol, side=PositionSide.LONG, entry_price=entry,
                   quantity=qty, strategy=strat)
    engine._positions[symbol] = pos
    return pos


def test_largest_position_pct_populated():
    e = PortfolioEngine()
    _open(e, "BTC-USDT", 100.0, 30.0)   # 3000 notional
    _open(e, "ETH-USDT", 50.0, 20.0)    # 1000 notional
    state = e.build_state(0.0)
    # largest notional 3000 / equity 10000 = 0.30
    assert abs(state.largest_position_pct - 0.30) < 1e-6


def test_var_es_zero_without_history():
    e = PortfolioEngine()
    _open(e, "BTC-USDT", 100.0, 30.0)
    state = e.build_state(0.0)
    assert state.portfolio_var_95 == 0.0
    assert state.expected_shortfall == 0.0


def test_var_es_populated_with_history():
    e = PortfolioEngine()
    _open(e, "BTC-USDT", 100.0, 50.0)   # 5000 exposure
    # Seed a realized-return distribution with a clear left tail.
    e._strategy_returns["strategy_1"] = [0.01] * 30 + [-0.05, -0.08, -0.10]
    state = e.build_state(0.0)
    assert state.portfolio_var_95 > 0.0
    # ES (mean of worst tail) must be at least as severe as VaR (the tail boundary).
    assert state.expected_shortfall >= state.portfolio_var_95


def test_no_positions_zeroed():
    e = PortfolioEngine()
    state = e.build_state(0.0)
    assert state.largest_position_pct == 0.0
    assert state.portfolio_var_95 == 0.0
