"""Live vs backfill isolation. Live metrics must not mix backfilled rows."""

from __future__ import annotations

LIVE_STATE_HISTORY_FLOOR = "2026-06-15"


class ProvenanceError(RuntimeError):
    pass


ALLOWED_LIVE = "observed_live"


def assert_live_provenance(provenance: str | None) -> None:
    if provenance != ALLOWED_LIVE:
        raise ProvenanceError(f"live performance queries accept only provenance={ALLOWED_LIVE!r}, got {provenance!r}")


def live_state_history_predicate(column: str = "timestamp") -> str:
    """SQL fragment callers must AND into live v2_state_history queries."""
    return f"{column} >= TIMESTAMPTZ '{LIVE_STATE_HISTORY_FLOOR}'"


def guard_live_query(*, provenance: str | None, uses_state_history: bool, sql: str) -> None:
    assert_live_provenance(provenance)
    if uses_state_history and LIVE_STATE_HISTORY_FLOOR not in sql:
        raise ProvenanceError(
            "live v2_state_history queries must bound "
            f"{LIVE_STATE_HISTORY_FLOOR}; mixing backfill is an error, not a docs note"
        )
