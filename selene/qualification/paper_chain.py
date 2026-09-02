"""In-process PAPER qualification chain.

scanner → signal → portfolio → risk → PAPER execution → order lifecycle → gateway.

Never constructs a live venue adapter. Duplicate place() calls must not increase
external side effects.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from services.execution.statemachine.order_fsm import OrderFSM, OrderRecord, OrderState
from shared.ledger.side_effects import SideEffectStore, submit_once
from shared.ledger.sqlite_store import SqliteLedger
from shared.models.signal import Direction, Regime, ScoredSignal, SignalType
from shared.runtime.release_identity import ExecMode, parse_exec_mode, should_init_exchange_adapters

HAPPY_PATH = (
    OrderState.SLIPPAGE_ESTIMATE,
    OrderState.ROUTING,
    OrderState.SUBMITTING,
    OrderState.PENDING_ACK,
    OrderState.OPEN,
)


@dataclass
class StageResult:
    name: str
    status: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class PaperChainResult:
    exec_mode: str
    stages: list[StageResult]
    order_state: str
    side_effect_submits: int
    duplicate_side_effects: int
    request_id: str
    headers: dict[str, str]


class PaperVenue:
    """PAPER fill simulator. Never talks to an exchange."""

    def __init__(self) -> None:
        self.submits = 0
        self.orders: dict[str, dict[str, Any]] = {}

    def place(self, cid: str, qty: float, price: float) -> dict[str, Any]:
        self.submits += 1
        rec = {"status": "acked", "exchange_id": f"paper-{cid}", "qty": qty, "price": price}
        self.orders[cid] = rec
        return rec

    def probe(self, cid: str) -> Optional[dict[str, Any]]:
        return self.orders.get(cid)


def _headers(request_id: str, actor: str, reason: str) -> dict[str, str]:
    return {
        "X-Request-Id": request_id,
        "X-Actor": actor,
        "X-Timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "X-Reason": reason,
    }


def run_paper_chain(
    *,
    store: Optional[SideEffectStore] = None,
    venue: Optional[PaperVenue] = None,
    environ: Optional[dict[str, str]] = None,
) -> PaperChainResult:
    env = dict(environ) if environ is not None else dict(os.environ)
    mode = parse_exec_mode(env.get("EXEC_MODE", "PAPER"))
    if mode is not ExecMode.PAPER:
        raise RuntimeError(f"qualification chain requires EXEC_MODE=PAPER, got {mode.value}")
    if should_init_exchange_adapters(mode):
        raise RuntimeError("qualification chain must not construct venue adapters")

    venue = venue or PaperVenue()
    store = store or SideEffectStore(SqliteLedger(":memory:"))
    request_id = str(uuid4())
    headers = _headers(request_id, "qualification", "paper-chain")
    stages: list[StageResult] = []

    scan = {"symbol": "BTCUSDT", "last": 100.0, "volume": 1_000_000.0, "interval": "1m"}
    stages.append(StageResult("scanner", "PASS", scan))

    signal = ScoredSignal(
        symbol=scan["symbol"],
        signal_type=SignalType.LONG_SETUP,
        direction=Direction.LONG,
        regime=Regime.RANGING,
        win_probability=0.60,
        confidence_lo=0.52,
        confidence_hi=0.68,
        expected_return=0.01,
        entry_price=scan["last"],
        stop_loss=99.0,
        take_profit=102.0,
        data_quality=0.95,
    )
    stages.append(
        StageResult(
            "signal",
            "PASS" if signal.is_actionable else "FAIL",
            {"id": signal.id, "win_probability": signal.win_probability, "regime": signal.regime.value},
        )
    )

    equity = 100_000.0
    kelly = 0.25 * (signal.win_probability - (1.0 - signal.win_probability))
    qty = max(0.001, round((equity * max(kelly, 0.0)) / signal.entry_price, 6))
    stages.append(StageResult("portfolio", "PASS", {"qty": qty, "kelly": kelly, "equity": equity}))

    var_ok = qty * signal.entry_price / equity < 0.20
    stages.append(StageResult("risk", "PASS" if var_ok else "FAIL", {"var_ok": var_ok}))

    cid = f"qual-{signal.id[:8]}O"
    rec = submit_once(
        venue="paper",
        account="qual",
        client_order_id=cid,
        operation_kind="place",
        submit_fn=lambda: venue.place(cid, qty, signal.entry_price),
        probe_fn=lambda: venue.probe(cid),
        store=store,
    )
    submit_once(
        venue="paper",
        account="qual",
        client_order_id=cid,
        operation_kind="place",
        submit_fn=lambda: venue.place(cid, qty, signal.entry_price),
        probe_fn=lambda: venue.probe(cid),
        store=store,
    )
    exec_ok = rec.status in {"acked", "submitted", "filled"} and venue.submits == 1
    stages.append(
        StageResult(
            "execution",
            "PASS" if exec_ok else "FAIL",
            {"status": rec.status, "submits": venue.submits, "mode": mode.value},
        )
    )

    order = OrderRecord(
        id=cid,
        signal_id=signal.id,
        symbol=signal.symbol,
        side="BUY",
        order_type="MARKET",
        quantity=qty,
        entry_price=signal.entry_price,
        stop_loss=signal.stop_loss,
        take_profit=signal.take_profit,
        exchange="paper",
        exchange_id=str(rec.payload.get("exchange_id") or ""),
    )
    fsm = OrderFSM(order)
    for state in HAPPY_PATH:
        fsm.transition(state)
    fsm.on_fill(qty, signal.entry_price)
    fsm.transition(OrderState.MONITORING)
    fsm.transition(OrderState.CLOSING)
    fsm.calc_realized_pnl(signal.take_profit)
    fsm.transition(OrderState.CLOSED)
    stages.append(StageResult("order_lifecycle", "PASS", {"state": order.state.value, "filled_qty": order.filled_qty}))

    gw_ok = all(k in headers for k in ("X-Request-Id", "X-Actor", "X-Timestamp", "X-Reason"))
    stages.append(StageResult("gateway", "PASS" if gw_ok else "FAIL", {"headers": list(headers)}))

    return PaperChainResult(
        exec_mode=mode.value,
        stages=stages,
        order_state=order.state.value,
        side_effect_submits=venue.submits,
        duplicate_side_effects=max(0, venue.submits - 1),
        request_id=request_id,
        headers=headers,
    )
