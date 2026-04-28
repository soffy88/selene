"""Core dataclasses for the paper trading module."""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from enum import Enum


class DecisionAction(str, Enum):
    OPEN_LONG = "open_long"
    OPEN_SHORT = "open_short"
    CLOSE = "close"
    HOLD = "hold"
    NO_ACTION = "no_action"


class PositionSide(str, Enum):
    LONG = "long"
    SHORT = "short"


@dataclass
class Decision:
    time: datetime
    symbol: str
    action: DecisionAction
    rule_id: str                          # which YAML rule triggered this
    config_hash: str                      # YAML config version hash
    current_state: Optional[str]
    previous_state: Optional[str]
    position_multiplier: Optional[float]  # from rule
    target_size_usdt: Optional[float]     # computed from base_size × multiplier × NAV
    reason: str


@dataclass
class SimulatedFill:
    time: datetime
    symbol: str
    side: PositionSide
    action: str                           # "open" or "close"
    requested_size_usdt: float
    fill_price: float
    fill_size_usdt: float                 # after slippage adjustment
    slippage_usdt: float
    fee_usdt: float
    net_size_usdt: float                  # fill_size_usdt - fee_usdt
    config_hash: str


@dataclass
class Position:
    symbol: str
    side: PositionSide
    open_time: datetime
    open_price: float
    size_usdt: float                      # initial position size
    current_size_usdt: float              # after partial closes
    unrealized_pnl_usdt: float = 0.0
    entry_fee_usdt: float = 0.0


@dataclass
class AccountState:
    time: datetime
    nav_usdt: float                       # net asset value
    realized_pnl_usdt: float
    unrealized_pnl_usdt: float
    position_side: Optional[str]          # None if no open position
    position_size_usdt: float
    daily_drawdown_pct: float
    consecutive_losses: int
    is_paused: bool                       # 24H pause from risk rules
    pause_until: Optional[datetime]
    cascade_cooling: bool                 # True if in post-cascade state
