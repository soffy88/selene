"""Gateway authentication, write-audit, and idempotency (P0-2)."""

from shared.security.audit import AuditLog, IdempotencyLedger, get_audit_log, get_ledger
from shared.security.auth import (
    GatewayAuthError,
    Principal,
    Role,
    WriteContext,
    assert_gateway_auth_ready,
    authenticate,
    require_role,
)

__all__ = [
    "AuditLog",
    "GatewayAuthError",
    "IdempotencyLedger",
    "Principal",
    "Role",
    "WriteContext",
    "assert_gateway_auth_ready",
    "authenticate",
    "get_audit_log",
    "get_ledger",
    "require_role",
]
