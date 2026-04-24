"""CryptoWatch v4 — Portfolio & Position models."""
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class PositionSide(str, Enum):
    LONG  = "LONG"
    SHORT = "SHORT"


class PositionStatus(str, Enum):
    OPEN    = "open"
    CLOSING = "closing"
    CLOSED  = "closed"


@dataclass
class Position:
    id:            str = field(default_factory=lambda: str(uuid.uuid4()))
    symbol:        str = ""
    side:          PositionSide = PositionSide.LONG
    entry_price:   float = 0.0
    quantity:      float = 0.0
    stop_loss:     float = 0.0
    take_profit:   float = 0.0
    strategy:      str = ""           # which strategy opened this
    signal_id:     str = ""
    status:        PositionStatus = PositionStatus.OPEN
    unrealized_pnl: float = 0.0
    realized_pnl:  Optional[float] = None
    exit_price:    Optional[float] = None
    close_reason:  Optional[str]  = None
    opened_at:     datetime = field(default_factory=datetime.utcnow)
    closed_at:     Optional[datetime] = None

    @property
    def notional(self) -> float:
        return self.entry_price * self.quantity

    @property
    def current_value(self) -> float:
        return (self.entry_price + self.unrealized_pnl / self.quantity) * self.quantity if self.quantity else 0.0

    def update_unrealized(self, current_price: float) -> None:
        if self.side == PositionSide.LONG:
            self.unrealized_pnl = (current_price - self.entry_price) * self.quantity
        else:
            self.unrealized_pnl = (self.entry_price - current_price) * self.quantity

    def to_dict(self) -> dict:
        return {
            "id": self.id, "symbol": self.symbol,
            "side": self.side.value, "entry_price": self.entry_price,
            "quantity": self.quantity, "notional": self.notional,
            "stop_loss": self.stop_loss, "take_profit": self.take_profit,
            "strategy": self.strategy, "status": self.status.value,
            "unrealized_pnl": self.unrealized_pnl, "realized_pnl": self.realized_pnl,
            "opened_at": self.opened_at.isoformat(),
        }


@dataclass
class PortfolioState:
    """
    Complete portfolio state snapshot.
    Published to Redis every 5s and on every position change.
    This is the single source of truth for Portfolio Service.
    """
    timestamp:          datetime = field(default_factory=datetime.utcnow)

    # Capital
    total_equity:       float = 0.0
    available_capital:  float = 0.0
    total_exposure:     float = 0.0    # sum of notional values
    leverage:           float = 0.0

    # Positions
    open_positions:     list = field(default_factory=list)  # list[Position]
    position_count:     int  = 0
    largest_position_pct: float = 0.0

    # Risk metrics
    portfolio_var_95:   float = 0.0    # 1-day 95% VaR in USD
    expected_shortfall: float = 0.0    # CVaR
    current_drawdown:   float = 0.0    # from peak (positive = drawdown)
    max_drawdown:       float = 0.0    # historical worst

    # Drawdown control level: GREEN / YELLOW / ORANGE / RED
    drawdown_level:     str   = "GREEN"
    max_position_allowed_pct: float = 1.0  # scales with drawdown level

    # Strategy allocations
    strategy_allocations: dict = field(default_factory=dict)
    risk_contributions:   dict = field(default_factory=dict)

    # PnL
    daily_pnl:          float = 0.0
    weekly_pnl:         float = 0.0
    total_pnl:          float = 0.0
    pnl_by_strategy:    dict  = field(default_factory=dict)
    pnl_by_signal_type: dict  = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp.isoformat(),
            "total_equity": self.total_equity,
            "available_capital": self.available_capital,
            "total_exposure": self.total_exposure,
            "leverage": self.leverage,
            "position_count": self.position_count,
            "largest_position_pct": self.largest_position_pct,
            "portfolio_var_95": self.portfolio_var_95,
            "expected_shortfall": self.expected_shortfall,
            "current_drawdown": self.current_drawdown,
            "max_drawdown": self.max_drawdown,
            "drawdown_level": self.drawdown_level,
            "max_position_allowed_pct": self.max_position_allowed_pct,
            "strategy_allocations": self.strategy_allocations,
            "risk_contributions": self.risk_contributions,
            "daily_pnl": self.daily_pnl,
            "total_pnl": self.total_pnl,
            "pnl_by_strategy": self.pnl_by_strategy,
        }
