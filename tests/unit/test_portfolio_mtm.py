"""Mark-to-market equity tests for PortfolioEngine (optimization item #5).

Sizing and the drawdown scalar previously used realized-only equity, so a deep
open-position drawdown still sized at GREEN/full. They now use MTM equity.
"""

import asyncio

from services.portfolio.main import INITIAL_CAPITAL, PortfolioEngine
from shared.models.portfolio import Position, PositionSide


def _open_long(engine, symbol, entry, qty):
    pos = Position(symbol=symbol, side=PositionSide.LONG, entry_price=entry, quantity=qty)
    engine._positions[symbol] = pos
    return pos


def test_equity_mtm_includes_open_pnl():
    e = PortfolioEngine()
    _open_long(e, "BTC-USDT", 100.0, 10.0)  # 1000 notional
    asyncio.run(e.mark_to_market({"BTC-USDT": 80.0}))  # -200 open PnL
    assert e._last_unrealized == -200.0
    assert e.equity_mtm == INITIAL_CAPITAL - 200.0


def test_drawdown_scalar_reacts_to_open_drawdown():
    e = PortfolioEngine()
    # Big position; a large adverse move creates >10% open drawdown.
    _open_long(e, "BTC-USDT", 100.0, 80.0)  # 8000 notional
    asyncio.run(e.mark_to_market({"BTC-USDT": 100.0}))  # flat → peak set at 10000
    assert e._drawdown_scalar() == 1.00
    asyncio.run(e.mark_to_market({"BTC-USDT": 84.0}))  # -1280 → 12.8% DD
    scalar = e._drawdown_scalar()
    assert scalar < 1.00
    assert scalar == 0.25  # 10% <= dd < 15% band


def test_peak_is_high_water_mark_on_mtm():
    e = PortfolioEngine()
    _open_long(e, "BTC-USDT", 100.0, 10.0)
    asyncio.run(e.mark_to_market({"BTC-USDT": 150.0}))  # +500 open → peak 10500
    assert e._peak_equity == INITIAL_CAPITAL + 500.0
    asyncio.run(e.mark_to_market({"BTC-USDT": 150.0}))  # unchanged
    assert e._peak_equity == INITIAL_CAPITAL + 500.0


def test_build_state_drawdown_uses_mtm():
    e = PortfolioEngine()
    _open_long(e, "BTC-USDT", 100.0, 50.0)  # 5000 notional
    unreal = asyncio.run(e.mark_to_market({"BTC-USDT": 100.0}))  # peak 10000
    unreal = asyncio.run(e.mark_to_market({"BTC-USDT": 90.0}))  # -500 → 5% DD
    state = e.build_state(unreal)
    assert state.current_drawdown > 0.0
    assert abs(state.total_equity - (INITIAL_CAPITAL - 500.0)) < 0.01
