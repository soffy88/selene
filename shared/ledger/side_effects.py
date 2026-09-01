"""Exactly-once-effect helper for venue operations (P0-4).

Flow: persist intent -> probe existing -> submit -> persist receipt.
A timeout never retries submit; it probes. Duplicate keys never call submit_fn again.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from shared.ledger.sqlite_store import SqliteLedger, get_sqlite_ledger

TERMINAL_OK = frozenset({"acked", "filled", "cancelled"})
IN_FLIGHT = frozenset({"intent", "submitted", "unknown"})


class DuplicateSideEffect(RuntimeError):
    def __init__(self, record: "SideEffectRecord"):
        super().__init__(
            f"duplicate side effect {record.venue}/{record.client_order_id}/{record.operation_kind}"
        )
        self.record = record


@dataclass
class SideEffectRecord:
    venue: str
    account: str
    client_order_id: str
    operation_kind: str
    status: str
    payload: dict[str, Any]


class SideEffectStore:
    def __init__(self, ledger: Optional[SqliteLedger] = None) -> None:
        self.ledger = ledger or get_sqlite_ledger()

    def probe(
        self, venue: str, account: str, client_order_id: str, operation_kind: str
    ) -> Optional[SideEffectRecord]:
        row = self.ledger.get_side_effect(venue, account, client_order_id, operation_kind)
        if row is None:
            return None
        import json

        payload = row["payload_json"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        return SideEffectRecord(
            venue=row["venue"],
            account=row["account"],
            client_order_id=row["client_order_id"],
            operation_kind=row["operation_kind"],
            status=row["status"],
            payload=payload or {},
        )

    def _write(self, rec: SideEffectRecord) -> SideEffectRecord:
        self.ledger.upsert_side_effect(
            venue=rec.venue,
            account=rec.account,
            client_order_id=rec.client_order_id,
            operation_kind=rec.operation_kind,
            status=rec.status,
            payload=rec.payload,
        )
        return rec


def submit_once(
    *,
    venue: str,
    account: str,
    client_order_id: str,
    operation_kind: str,
    submit_fn: Callable[[], dict[str, Any]],
    probe_fn: Optional[Callable[[], Optional[dict[str, Any]]]] = None,
    store: Optional[SideEffectStore] = None,
) -> SideEffectRecord:
    """Submit at most once. Existing acked/submitted records short-circuit without calling submit_fn."""
    store = store or SideEffectStore()
    existing = store.probe(venue, account, client_order_id, operation_kind)
    if existing is not None and existing.status in (TERMINAL_OK | {"submitted", "intent", "unknown"}):
        # Never call submit_fn again for a key that already left a durable record.
        return existing

    rec = SideEffectRecord(
        venue=venue,
        account=account,
        client_order_id=client_order_id,
        operation_kind=operation_kind,
        status="intent",
        payload={},
    )
    store._write(rec)

    try:
        result = submit_fn()
    except TimeoutError:
        rec.status = "unknown"
        rec.payload = {"error": "timeout_waiting_ack"}
        store._write(rec)
        if probe_fn is not None:
            probed = probe_fn()
            if probed:
                rec.status = str(probed.get("status") or "acked")
                rec.payload = probed
                store._write(rec)
                return rec
        return rec

    rec.status = str(result.get("status") or "submitted")
    rec.payload = result
    store._write(rec)
    return rec
