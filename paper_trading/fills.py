"""Fill simulator — applies slippage + fees to produce a SimulatedFill."""
from datetime import datetime
from typing import Optional

from decision.config import ExecutionConfig
from paper_trading.schema import Position, PositionSide, SimulatedFill


class FillSimulator:
    def __init__(self, config: ExecutionConfig) -> None:
        self.config = config

    # ------------------------------------------------------------------
    def simulate_open(
        self,
        time: datetime,
        symbol: str,
        side: PositionSide,
        size_usdt: float,
        mark_price: float,
        config_hash: str,
    ) -> SimulatedFill:
        """Apply taker fee + open slippage."""
        fee_rate = (
            self.config.taker_fee_pct
            if self.config.assume_taker
            else self.config.maker_fee_pct
        )
        slippage_pct = self.config.slippage_open_pct

        # Adverse fill: long pays more, short gets less
        if side == PositionSide.LONG:
            fill_price = mark_price * (1 + slippage_pct)
        else:
            fill_price = mark_price * (1 - slippage_pct)

        fill_size_usdt = size_usdt
        slippage_usdt = fill_size_usdt * slippage_pct
        fee_usdt = fill_size_usdt * fee_rate
        net_size_usdt = fill_size_usdt - fee_usdt

        return SimulatedFill(
            time=time,
            symbol=symbol,
            side=side,
            action="open",
            requested_size_usdt=size_usdt,
            fill_price=fill_price,
            fill_size_usdt=fill_size_usdt,
            slippage_usdt=slippage_usdt,
            fee_usdt=fee_usdt,
            net_size_usdt=net_size_usdt,
            config_hash=config_hash,
        )

    # ------------------------------------------------------------------
    def simulate_close(
        self,
        time: datetime,
        symbol: str,
        position: Position,
        mark_price: float,
        config_hash: str,
        size_to_close_usdt: Optional[float] = None,
    ) -> tuple[SimulatedFill, float]:
        """Return (fill, realized_pnl_usdt).

        PnL = (exit_price - entry_price) / entry_price × close_size (long)
              (entry_price - exit_price) / entry_price × close_size (short)
        """
        close_size = (
            size_to_close_usdt
            if size_to_close_usdt is not None
            else position.current_size_usdt
        )

        fee_rate = (
            self.config.taker_fee_pct
            if self.config.assume_taker
            else self.config.maker_fee_pct
        )
        slippage_pct = self.config.slippage_close_pct

        # Adverse fill on close: long sells lower, short buys higher
        if position.side == PositionSide.LONG:
            fill_price = mark_price * (1 - slippage_pct)
        else:
            fill_price = mark_price * (1 + slippage_pct)

        slippage_usdt = close_size * slippage_pct
        fee_usdt = close_size * fee_rate
        net_size_usdt = close_size - fee_usdt

        # Gross PnL (before fees, after slippage in price)
        if position.side == PositionSide.LONG:
            pnl = (fill_price - position.open_price) / position.open_price * close_size
        else:
            pnl = (position.open_price - fill_price) / position.open_price * close_size

        # Net realized PnL (after exit fee; entry fee already deducted at open)
        realized_pnl = pnl - fee_usdt

        fill = SimulatedFill(
            time=time,
            symbol=symbol,
            side=position.side,
            action="close",
            requested_size_usdt=close_size,
            fill_price=fill_price,
            fill_size_usdt=close_size,
            slippage_usdt=slippage_usdt,
            fee_usdt=fee_usdt,
            net_size_usdt=net_size_usdt,
            config_hash=config_hash,
        )
        return fill, realized_pnl
