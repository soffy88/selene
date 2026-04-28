"""PositionTracker — open / close / reduce positions."""
from datetime import datetime
from typing import Optional

from paper_trading.schema import Position, PositionSide, SimulatedFill


class PositionTracker:
    def __init__(self) -> None:
        self._position: Optional[Position] = None

    # ------------------------------------------------------------------
    def open(self, fill: SimulatedFill, bar_time: datetime, mark_price: float) -> Position:
        """Open a new position from a fill. Raises if a position is already open."""
        if self._position is not None:
            raise RuntimeError(
                f"Cannot open position: {self._position.symbol} {self._position.side} already open"
            )
        self._position = Position(
            symbol=fill.symbol,
            side=fill.side,
            open_time=bar_time,
            open_price=fill.fill_price,
            size_usdt=fill.net_size_usdt,
            current_size_usdt=fill.net_size_usdt,
            unrealized_pnl_usdt=0.0,
            entry_fee_usdt=fill.fee_usdt,
        )
        self.update_unrealized(mark_price)
        return self._position

    # ------------------------------------------------------------------
    def close(self, fill: SimulatedFill, realized_pnl: float) -> None:
        """Close the current position entirely."""
        if self._position is None:
            raise RuntimeError("No open position to close")
        self._position = None

    # ------------------------------------------------------------------
    def reduce(self, fill: SimulatedFill, realized_pnl: float) -> None:
        """Reduce the current position by the fill's size (partial close)."""
        if self._position is None:
            raise RuntimeError("No open position to reduce")
        self._position.current_size_usdt -= fill.fill_size_usdt
        if self._position.current_size_usdt < 0:
            self._position.current_size_usdt = 0.0

    # ------------------------------------------------------------------
    def update_unrealized(self, current_price: float) -> None:
        """Update unrealized PnL based on the current mark price."""
        if self._position is None:
            return
        pos = self._position
        if pos.side == PositionSide.LONG:
            pnl = (current_price - pos.open_price) / pos.open_price * pos.current_size_usdt
        else:
            pnl = (pos.open_price - current_price) / pos.open_price * pos.current_size_usdt
        pos.unrealized_pnl_usdt = pnl

    # ------------------------------------------------------------------
    @property
    def current(self) -> Optional[Position]:
        return self._position
