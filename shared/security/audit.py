"""Write-side audit log and idempotency ledger. Never stores secrets."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class AuditEvent:
    kind: str
    actor: str
    role: str
    request_id: str
    reason: str
    path: str
    git_sha: str
    payload: dict[str, Any] = field(default_factory=dict)
    at: str = field(default_factory=_utcnow)


class AuditLog:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def record(self, event: AuditEvent) -> AuditEvent:
        self.events.append(event)
        return event

    def reset(self) -> None:
        self.events.clear()


class IdempotencyLedger:
    """Maps request_id -> first successful write response. Durable via SQLite."""

    def __init__(self) -> None:
        from shared.ledger.sqlite_store import get_sqlite_ledger

        self._store = get_sqlite_ledger()

    def lookup(self, request_id: str) -> Optional[dict[str, Any]]:
        return self._store.lookup_write(request_id)

    def remember(self, request_id: str, *, status_code: int, body: Any, path: str, actor: str) -> None:
        self._store.remember_write(
            request_id, status_code=status_code, body=body, path=path, actor=actor
        )

    def reset(self) -> None:
        import shared.ledger.sqlite_store as store_mod
        from shared.ledger.sqlite_store import SqliteLedger

        store_mod._DEFAULT = SqliteLedger(":memory:")
        self._store = store_mod._DEFAULT


_AUDIT = AuditLog()
_LEDGER = IdempotencyLedger()


def get_audit_log() -> AuditLog:
    return _AUDIT


def get_ledger() -> IdempotencyLedger:
    return _LEDGER


def record_write(*, ctx: Any, git_sha: str = "unknown", extra: Optional[dict[str, Any]] = None) -> AuditEvent:
    event = AuditEvent(
        kind="write",
        actor=getattr(ctx, "actor", "unknown"),
        role=getattr(getattr(ctx, "principal", None), "role", "unknown").value
        if getattr(getattr(ctx, "principal", None), "role", None) is not None
        else "unknown",
        request_id=getattr(ctx, "request_id", ""),
        reason=getattr(ctx, "reason", ""),
        path=getattr(ctx, "path", ""),
        git_sha=git_sha,
        payload=extra or {},
    )
    return _AUDIT.record(event)


def record_halt_reset(
    *,
    ctx: Any,
    old_state: str,
    new_state: str,
    git_sha: str,
) -> AuditEvent:
    event = AuditEvent(
        kind="halt_reset",
        actor=getattr(ctx, "actor", "unknown"),
        role=getattr(getattr(ctx, "principal", None), "role", "unknown").value
        if getattr(getattr(ctx, "principal", None), "role", None) is not None
        else "unknown",
        request_id=getattr(ctx, "request_id", ""),
        reason=getattr(ctx, "reason", ""),
        path=getattr(ctx, "path", ""),
        git_sha=git_sha,
        payload={"old_state": old_state, "new_state": new_state},
    )
    return _AUDIT.record(event)
