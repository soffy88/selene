"""Qualification fault injection: duplicates, out-of-order fills, timeout, partial, restart."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from typing import Any, Optional

from services.execution.statemachine.order_fsm import OrderFSM, OrderRecord, OrderState
from shared.ledger.side_effects import SideEffectStore, submit_once
from shared.ledger.sqlite_store import SqliteLedger

RECOVERABLE = frozenset(
    {
        OrderState.OPEN,
        OrderState.PARTIALLY_FILLED,
        OrderState.MONITORING,
        OrderState.CLOSING,
        OrderState.SUBMITTING,
        OrderState.PENDING_ACK,
        OrderState.QUARANTINED,
    }
)


@dataclass
class FaultCase:
    name: str
    status: str
    detail: dict[str, Any] = field(default_factory=dict)


class CountingVenue:
    def __init__(self) -> None:
        self.submits = 0
        self.orders: dict[str, dict[str, Any]] = {}
        self.timeout_once = False

    def place(self, cid: str) -> dict[str, Any]:
        self.submits += 1
        rec = {"status": "acked", "exchange_id": f"paper-{cid}"}
        self.orders[cid] = rec
        if self.timeout_once:
            self.timeout_once = False
            raise TimeoutError("ack lost")
        return rec

    def probe(self, cid: str) -> Optional[dict[str, Any]]:
        return self.orders.get(cid)


def recover_order(record: OrderRecord) -> OrderRecord:
    """Reload-safe recovery. Unknown in-flight place without receipt is quarantined."""
    if record.state not in RECOVERABLE:
        return record
    if record.state in {OrderState.SUBMITTING, OrderState.PENDING_ACK} and not record.exchange_id:
        fsm = OrderFSM(record)
        fsm.transition(OrderState.QUARANTINED, note="restart_without_ack")
    return record


def run_faults(*, ledger_path: str | None = None) -> list[FaultCase]:
    cleanup = None
    if not ledger_path or ledger_path == ":memory:":
        handle, ledger_path = tempfile.mkstemp(prefix="selene-qual-", suffix=".sqlite")
        os.close(handle)
        cleanup = ledger_path
    cases: list[FaultCase] = []
    venue = CountingVenue()
    store = SideEffectStore(SqliteLedger(ledger_path))

    cid = "fault-dup-1O"
    submit_once(
        venue="paper",
        account="qual",
        client_order_id=cid,
        operation_kind="place",
        submit_fn=lambda: venue.place(cid),
        probe_fn=lambda: venue.probe(cid),
        store=store,
    )
    submit_once(
        venue="paper",
        account="qual",
        client_order_id=cid,
        operation_kind="place",
        submit_fn=lambda: venue.place(cid),
        probe_fn=lambda: venue.probe(cid),
        store=store,
    )
    cases.append(
        FaultCase(
            "duplicate_message",
            "PASS" if venue.submits == 1 else "FAIL",
            {"submits": venue.submits},
        )
    )

    venue.timeout_once = True
    cid_t = "fault-timeout-1O"
    rec = submit_once(
        venue="paper",
        account="qual",
        client_order_id=cid_t,
        operation_kind="place",
        submit_fn=lambda: venue.place(cid_t),
        probe_fn=lambda: venue.probe(cid_t),
        store=store,
    )
    before = venue.submits
    submit_once(
        venue="paper",
        account="qual",
        client_order_id=cid_t,
        operation_kind="place",
        submit_fn=lambda: venue.place(cid_t),
        probe_fn=lambda: venue.probe(cid_t),
        store=store,
    )
    cases.append(
        FaultCase(
            "timeout_no_resubmit",
            "PASS" if rec.status == "acked" and venue.submits == before else "FAIL",
            {"status": rec.status, "submits": venue.submits, "before": before},
        )
    )

    order = OrderRecord(id="oo-1", symbol="BTCUSDT", side="BUY", quantity=2.0, entry_price=100.0)
    fsm = OrderFSM(order)
    for state in (
        OrderState.SLIPPAGE_ESTIMATE,
        OrderState.ROUTING,
        OrderState.SUBMITTING,
        OrderState.PENDING_ACK,
        OrderState.OPEN,
    ):
        fsm.transition(state)
    fsm.on_fill(0.5, 100.1)
    fsm.on_fill(1.5, 99.9)
    cases.append(
        FaultCase(
            "out_of_order_fills",
            "PASS" if order.state is OrderState.FILLED and abs(order.filled_qty - 2.0) < 1e-9 else "FAIL",
            {"state": order.state.value, "filled_qty": order.filled_qty},
        )
    )

    partial = OrderRecord(id="pf-1", symbol="BTCUSDT", side="BUY", quantity=2.0, entry_price=100.0)
    pfsm = OrderFSM(partial)
    for state in (
        OrderState.SLIPPAGE_ESTIMATE,
        OrderState.ROUTING,
        OrderState.SUBMITTING,
        OrderState.PENDING_ACK,
        OrderState.OPEN,
    ):
        pfsm.transition(state)
    pfsm.on_fill(0.75, 100.0)
    cases.append(
        FaultCase(
            "partial_fill",
            "PASS" if partial.state is OrderState.PARTIALLY_FILLED else "FAIL",
            {"state": partial.state.value, "filled_qty": partial.filled_qty},
        )
    )

    inflight = OrderRecord(id="rs-1", symbol="BTCUSDT", side="BUY", quantity=1.0, state=OrderState.PENDING_ACK)
    recovered = recover_order(inflight)
    cases.append(
        FaultCase(
            "process_restart_inflight",
            "PASS" if recovered.state is OrderState.QUARANTINED else "FAIL",
            {"state": recovered.state.value},
        )
    )

    open_rec = OrderRecord(
        id="rs-2",
        symbol="BTCUSDT",
        side="BUY",
        quantity=1.0,
        filled_qty=1.0,
        exchange_id="paper-rs-2",
        state=OrderState.OPEN,
    )
    recovered_open = recover_order(open_rec)
    cases.append(
        FaultCase(
            "process_restart_open",
            "PASS" if recovered_open.state is OrderState.OPEN else "FAIL",
            {"state": recovered_open.state.value},
        )
    )

    store2 = SideEffectStore(SqliteLedger(ledger_path))
    again = store2.probe("paper", "qual", cid, "place")
    cases.append(
        FaultCase(
            "ledger_restart",
            "PASS" if again is not None and again.status == "acked" else "FAIL",
            {"found": again.status if again else None},
        )
    )
    if cleanup:
        try:
            os.unlink(cleanup)
        except OSError:
            pass
    return cases
