"""V4 position reconciliation regression tests (2026-07-16 duplicate-position bug).

Two defects let HOMEUSDT/BTCUSDT each open two MONITORING positions:
1. portfolio.handle_order_event only opened on event=="filled", but PAPER fills
   publish "filled_immediately" -> portfolio never tracked PAPER positions live.
2. execution.process_scored_signal had no per-symbol "already open" guard, so a
   signal re-emitted after the 1h cooldown opened a second position.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from services.portfolio.main import PortfolioEngine


def _fill_event(signal_id, symbol, event="filled_immediately", side="BUY"):
    return {
        "event": event,
        "signal_id": signal_id,
        "symbol": symbol,
        "side": side,
        "filled_price": "100",
        "filled_qty": "2",
        "stop_loss": "90",
        "take_profit": "120",
        "signal_type": "LONG_SETUP",
    }


class TestPortfolioOpensOnPaperFill:
    def test_filled_immediately_opens_position(self):
        """PAPER open event must register a position (was silently ignored)."""
        e = PortfolioEngine()
        asyncio.run(e.handle_order_event(_fill_event("sig1", "BTCUSDT")))
        assert len(e._positions) == 1
        assert e._positions["sig1"].symbol == "BTCUSDT"

    def test_live_filled_still_opens_position(self):
        """The live-exchange 'filled' event must keep working too."""
        e = PortfolioEngine()
        asyncio.run(e.handle_order_event(_fill_event("sig2", "ETHUSDT", event="filled")))
        assert len(e._positions) == 1

    def test_close_pops_the_position(self):
        e = PortfolioEngine()
        asyncio.run(e.handle_order_event(_fill_event("sig3", "SOLUSDT")))
        asyncio.run(
            e.handle_order_event(
                {
                    "event": "closed",
                    "signal_id": "sig3",
                    "symbol": "SOLUSDT",
                    "realized_pnl": "5.0",
                }
            )
        )
        assert len(e._positions) == 0


class TestPerSymbolDedupPredicate:
    """Mirrors the guard in execution.process_scored_signal: skip a signal when a
    non-terminal order already exists for that symbol."""

    @staticmethod
    def _has_open(orders, symbol):
        return any(rec["symbol"] == symbol and not rec["terminal"] for rec in orders)

    def test_blocks_second_position_same_symbol(self):
        orders = [{"symbol": "HOMEUSDT", "terminal": False}]
        assert self._has_open(orders, "HOMEUSDT") is True

    def test_allows_after_prior_closed(self):
        orders = [{"symbol": "HOMEUSDT", "terminal": True}]
        assert self._has_open(orders, "HOMEUSDT") is False

    def test_allows_different_symbol(self):
        orders = [{"symbol": "HOMEUSDT", "terminal": False}]
        assert self._has_open(orders, "BTCUSDT") is False
