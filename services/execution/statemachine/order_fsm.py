"""
CryptoWatch v4 — Order State Machine (13 states including error terminals).
D2 Fix: Complete order lifecycle. Every transition is explicit and logged.
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class OrderState(str, Enum):
    QUEUED = "QUEUED"
    SLIPPAGE_ESTIMATE = "SLIPPAGE_ESTIMATE"
    ROUTING = "ROUTING"
    SUBMITTING = "SUBMITTING"
    PENDING_ACK = "PENDING_ACK"
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    MONITORING = "MONITORING"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    QUARANTINED = "QUARANTINED"


VALID_TRANSITIONS = {
    OrderState.QUEUED: {OrderState.SLIPPAGE_ESTIMATE: "Begin slippage", OrderState.CANCELLED: "Queue timeout"},
    OrderState.SLIPPAGE_ESTIMATE: {
        OrderState.ROUTING: "Slippage ok",
        OrderState.QUEUED: "Re-queue",
        OrderState.CANCELLED: "Slippage too high",
    },
    OrderState.ROUTING: {OrderState.SUBMITTING: "Route found", OrderState.FAILED: "No route"},
    OrderState.SUBMITTING: {OrderState.PENDING_ACK: "Sent", OrderState.FAILED: "API error"},
    OrderState.PENDING_ACK: {OrderState.OPEN: "Confirmed", OrderState.FAILED: "Timeout"},
    OrderState.OPEN: {
        OrderState.PARTIALLY_FILLED: "Partial",
        OrderState.FILLED: "Full fill",
        OrderState.CANCELLED: "Cancelled",
    },
    OrderState.PARTIALLY_FILLED: {
        OrderState.FILLED: "Complete",
        OrderState.CANCELLED: "Partial cancel",
        OrderState.MONITORING: "Accept partial",
    },
    OrderState.FILLED: {OrderState.MONITORING: "Enter monitoring"},
    OrderState.MONITORING: {OrderState.CLOSING: "Exit triggered"},
    OrderState.CLOSING: {OrderState.CLOSED: "Exit filled", OrderState.MONITORING: "Exit unfilled"},
    OrderState.CLOSED: {},
    OrderState.CANCELLED: {},
    OrderState.FAILED: {},
    OrderState.QUARANTINED: {},
    OrderState.SUBMITTING: {
        OrderState.PENDING_ACK: "Sent",
        OrderState.FAILED: "API error",
        OrderState.QUARANTINED: "unsafe to retry",
    },
    OrderState.PENDING_ACK: {
        OrderState.OPEN: "Confirmed",
        OrderState.FAILED: "Timeout",
        OrderState.QUARANTINED: "ack lost",
    },
    OrderState.OPEN: {
        OrderState.PARTIALLY_FILLED: "Partial",
        OrderState.FILLED: "Full fill",
        OrderState.CANCELLED: "Cancelled",
        OrderState.QUARANTINED: "reconcile diverge",
    },
    OrderState.PARTIALLY_FILLED: {
        OrderState.FILLED: "Complete",
        OrderState.CANCELLED: "Partial cancel",
        OrderState.MONITORING: "Accept partial",
        OrderState.QUARANTINED: "reconcile diverge",
    },
    OrderState.MONITORING: {OrderState.CLOSING: "Exit triggered", OrderState.QUARANTINED: "reconcile diverge"},
}

TERMINAL_STATES = {OrderState.CLOSED, OrderState.CANCELLED, OrderState.FAILED, OrderState.QUARANTINED}


@dataclass
class OrderRecord:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    signal_id: str = ""
    symbol: str = ""
    side: str = ""
    order_type: str = ""
    quantity: float = 0.0
    entry_price: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    limit_price: Optional[float] = None
    filled_qty: float = 0.0
    filled_price: float = 0.0
    slippage_pct: float = 0.0
    fee_paid: float = 0.0
    exchange: str = ""
    exchange_id: str = ""
    kelly_fraction: float = 0.0
    risk_usd: float = 0.0
    state: OrderState = OrderState.QUEUED
    history: list = field(default_factory=list)
    reject_reason: str = ""
    close_reason: str = ""
    realized_pnl: Optional[float] = None
    exit_price: Optional[float] = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    closed_at: Optional[datetime] = None

    @property
    def is_terminal(self):
        return self.state in TERMINAL_STATES

    @property
    def notional(self):
        return self.filled_price * self.filled_qty if self.filled_qty else self.entry_price * self.quantity


class OrderFSM:
    def __init__(self, record: OrderRecord):
        self.record = record

    def transition(self, new_state: OrderState, note: str = "") -> None:
        current = self.record.state
        allowed = VALID_TRANSITIONS.get(current, {})
        if new_state not in allowed:
            raise ValueError(
                f"Invalid transition: {current.value} → {new_state.value} "
                f"(order {self.record.id[:8]}). Allowed: {[s.value for s in allowed]}"
            )
        self.record.state = new_state
        self.record.history.append(
            {
                "from": current.value,
                "to": new_state.value,
                "description": allowed[new_state],
                "note": note,
                "timestamp": datetime.utcnow().isoformat(),
            }
        )
        if new_state == OrderState.CLOSED:
            self.record.closed_at = datetime.utcnow()
        logger.info(f"Order {self.record.id[:8]} [{self.record.symbol}]: {current.value} → {new_state.value}")

    def on_fill(self, qty: float, price: float, fee: float = 0.0) -> None:
        self.record.fee_paid += fee
        prev_qty = self.record.filled_qty
        self.record.filled_qty += qty
        if self.record.filled_qty > 0:
            self.record.filled_price = (self.record.filled_price * prev_qty + price * qty) / self.record.filled_qty
        if self.record.entry_price > 0:
            self.record.slippage_pct = (
                (self.record.filled_price - self.record.entry_price) / self.record.entry_price * 100
            )
        remaining = self.record.quantity - self.record.filled_qty
        if remaining <= 0.000001:
            self.transition(OrderState.FILLED, f"Full fill @ {price:.4f}")
        elif self.record.state == OrderState.OPEN:
            self.transition(OrderState.PARTIALLY_FILLED, f"Partial {qty:.4f}/{self.record.quantity} @ {price:.4f}")

    def calc_realized_pnl(self, exit_price: float) -> float:
        if self.record.side == "BUY":
            pnl = (exit_price - self.record.filled_price) * self.record.filled_qty
        else:
            pnl = (self.record.filled_price - exit_price) * self.record.filled_qty
        pnl -= self.record.fee_paid
        self.record.realized_pnl = round(pnl, 8)
        self.record.exit_price = exit_price
        return self.record.realized_pnl

    def to_dict(self) -> dict:
        r = self.record
        return {
            "id": r.id,
            "signal_id": r.signal_id,
            "symbol": r.symbol,
            "side": r.side,
            "order_type": r.order_type,
            "quantity": r.quantity,
            "filled_qty": r.filled_qty,
            "entry_price": r.entry_price,
            "filled_price": r.filled_price,
            "stop_loss": r.stop_loss,
            "take_profit": r.take_profit,
            "slippage_pct": r.slippage_pct,
            "fee_paid": r.fee_paid,
            "state": r.state.value,
            "reject_reason": r.reject_reason,
            "realized_pnl": r.realized_pnl,
            "exit_price": r.exit_price,
            "kelly_fraction": r.kelly_fraction,
            "created_at": r.created_at.isoformat(),
            "closed_at": r.closed_at.isoformat() if r.closed_at else None,
            "history": r.history,
        }
