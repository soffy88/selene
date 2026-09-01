import pytest

from shared.data.provenance import (
    LIVE_STATE_HISTORY_FLOOR,
    ProvenanceError,
    guard_live_query,
    live_state_history_predicate,
)


def test_backfill_cannot_enter_live_metrics():
    with pytest.raises(ProvenanceError):
        guard_live_query(provenance="backfilled", uses_state_history=True, sql="SELECT 1")


def test_live_query_without_floor_errors():
    with pytest.raises(ProvenanceError, match="2026-06-15"):
        guard_live_query(
            provenance="observed_live",
            uses_state_history=True,
            sql="SELECT * FROM v2_state_history",
        )


def test_live_query_with_floor_ok():
    sql = f"SELECT * FROM v2_state_history WHERE {live_state_history_predicate()}"
    assert LIVE_STATE_HISTORY_FLOOR in sql
    guard_live_query(provenance="observed_live", uses_state_history=True, sql=sql)
