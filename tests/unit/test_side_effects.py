"""P0-4 exactly-once venue side effects + timeout probe (no duplicate submit)."""

from __future__ import annotations

from shared.ledger.side_effects import SideEffectStore, submit_once
from shared.ledger.sqlite_store import SqliteLedger


class FakeVenue:
    def __init__(self) -> None:
        self.submits = 0
        self.orders: dict[str, dict] = {}
        self.drop_ack = False
        self.fail_next = False

    def submit(self, cid: str) -> dict:
        self.submits += 1
        if self.fail_next:
            self.fail_next = False
            raise TimeoutError("ack lost")
        if self.drop_ack:
            self.orders[cid] = {"status": "acked", "exchange_id": f"ex-{cid}"}
            raise TimeoutError("ack lost")
        rec = {"status": "acked", "exchange_id": f"ex-{cid}"}
        self.orders[cid] = rec
        return rec

    def probe(self, cid: str):
        return self.orders.get(cid)


def test_duplicate_submit_does_not_call_venue_twice():
    store = SideEffectStore(SqliteLedger(":memory:"))
    venue = FakeVenue()
    first = submit_once(
        venue="binance",
        account="sub-1",
        client_order_id="oid-1O",
        operation_kind="place",
        submit_fn=lambda: venue.submit("oid-1O"),
        store=store,
    )
    second = submit_once(
        venue="binance",
        account="sub-1",
        client_order_id="oid-1O",
        operation_kind="place",
        submit_fn=lambda: venue.submit("oid-1O"),
        store=store,
    )
    assert first.status == "acked"
    assert second.status == "acked"
    assert venue.submits == 1


def test_timeout_probes_and_does_not_resubmit():
    store = SideEffectStore(SqliteLedger(":memory:"))
    venue = FakeVenue()
    venue.drop_ack = True
    rec = submit_once(
        venue="binance",
        account="sub-1",
        client_order_id="oid-2O",
        operation_kind="place",
        submit_fn=lambda: venue.submit("oid-2O"),
        probe_fn=lambda: venue.probe("oid-2O"),
        store=store,
    )
    assert rec.status == "acked"
    assert venue.submits == 1
    again = submit_once(
        venue="binance",
        account="sub-1",
        client_order_id="oid-2O",
        operation_kind="place",
        submit_fn=lambda: venue.submit("oid-2O"),
        probe_fn=lambda: venue.probe("oid-2O"),
        store=store,
    )
    assert venue.submits == 1
    assert again.status == "acked"


def test_timeout_without_probe_stays_unknown_no_retry():
    store = SideEffectStore(SqliteLedger(":memory:"))
    venue = FakeVenue()
    venue.fail_next = True
    rec = submit_once(
        venue="okx",
        account="sub-1",
        client_order_id="oid-3O",
        operation_kind="place",
        submit_fn=lambda: venue.submit("oid-3O"),
        store=store,
    )
    assert rec.status == "unknown"
    assert venue.submits == 1
