"""
Tests for sel_v2.strategies.db_writer — all DB calls mocked via asyncpg.

Covers:
- write_state: correct SQL parameters, graceful failure on error
- write_cusum_event: correct parameters
- write_strategy2_decision: maps EntryDecision fields correctly
- write_inverse_vocab_event: Wave 5 placeholder interface
- write_states_bulk: executemany call
- clear_state_history: truncate call
- No DB connection: write_* methods return False gracefully
"""
import json
import pytest
from datetime import datetime, timezone
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch, call

from sel_v2.states.schema import StateLabel, StateRecord
from sel_v2.strategies.db_writer import DBWriter

_TS = datetime(2025, 6, 1, 12, 0, tzinfo=timezone.utc)


def _make_state_record(
    state: StateLabel = StateLabel.COILING,
    transition_from: Optional[StateLabel] = None,
    transition_via: Optional[str] = None,
    cold_start: bool = False,
) -> StateRecord:
    return StateRecord(
        timestamp=_TS,
        state=state,
        sub_state=None,
        state_features={"sigma_pctile": 0.25, "cold_start": cold_start},
        transition_from=transition_from,
        transition_via=transition_via,
        duration_4h=3,
        cold_start=cold_start,
        none_reason="entropy_4h" if cold_start else None,
        is_legal=True,
    )


# ── Connection management ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_connect_calls_asyncpg():
    with patch("sel_v2.strategies.db_writer.asyncpg.connect", new_callable=AsyncMock) as mock_conn:
        mock_conn.return_value = AsyncMock()
        writer = DBWriter(db_url="postgresql://test:test@localhost:5432/test")
        await writer.connect()
        mock_conn.assert_called_once()


@pytest.mark.asyncio
async def test_close_closes_connection():
    writer = DBWriter()
    mock_conn = AsyncMock()
    writer._conn = mock_conn
    await writer.close()
    mock_conn.close.assert_called_once()
    assert writer._conn is None


# ── write_state ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_write_state_calls_execute():
    writer = DBWriter()
    mock_conn = AsyncMock()
    writer._conn = mock_conn

    record = _make_state_record(
        state=StateLabel.COILING,
        transition_from=None,
        transition_via=None,
    )
    result = await writer.write_state(record)

    assert result is True
    mock_conn.execute.assert_called_once()
    call_args = mock_conn.execute.call_args[0]
    # SQL is first arg, then positional params
    assert "INSERT INTO v2_state_history" in call_args[0]
    assert call_args[1] == _TS
    assert call_args[2] == "Coiling"
    assert call_args[5] is None   # transition_from = None
    assert call_args[6] is None   # transition_via = None


@pytest.mark.asyncio
async def test_write_state_with_transition():
    writer = DBWriter()
    writer._conn = AsyncMock()
    record = _make_state_record(
        state=StateLabel.SURGING,
        transition_from=StateLabel.COILING,
        transition_via="Release",
    )
    await writer.write_state(record)
    call_args = writer._conn.execute.call_args[0]
    assert call_args[2] == "Surging"
    assert call_args[5] == "Coiling"
    assert call_args[6] == "Release"


@pytest.mark.asyncio
async def test_write_state_returns_false_on_exception():
    writer = DBWriter()
    mock_conn = AsyncMock()
    mock_conn.execute.side_effect = Exception("DB error")
    writer._conn = mock_conn
    result = await writer.write_state(_make_state_record())
    assert result is False  # graceful failure, no exception raised


@pytest.mark.asyncio
async def test_write_state_returns_false_when_not_connected():
    writer = DBWriter()
    # _conn is None by default
    result = await writer.write_state(_make_state_record())
    assert result is False


# ── write_cusum_event ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_write_cusum_event_correct_params():
    writer = DBWriter()
    writer._conn = AsyncMock()
    result = await writer.write_cusum_event(
        timestamp=_TS,
        cusum_type="short",
        direction="up",
        peak_value=3.14,
        threshold_h=2.0,
        z_returns_window=[1.2, 0.9, 1.5],
    )
    assert result is True
    call_args = writer._conn.execute.call_args[0]
    assert "INSERT INTO v2_cusum_events" in call_args[0]
    assert call_args[2] == "short"
    assert call_args[3] == "up"
    assert call_args[4] == 3.14


@pytest.mark.asyncio
async def test_write_cusum_event_graceful_failure():
    writer = DBWriter()
    writer._conn = AsyncMock()
    writer._conn.execute.side_effect = Exception("timeout")
    result = await writer.write_cusum_event(
        _TS, "short", "down", 2.5, 1.8
    )
    assert result is False


@pytest.mark.asyncio
async def test_write_cusum_event_no_connection():
    writer = DBWriter()
    result = await writer.write_cusum_event(_TS, "short", "up", 1.0, 2.0)
    assert result is False


# ── write_strategy2_decision ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_write_strategy2_decision_enter_long():
    writer = DBWriter()
    writer._conn = AsyncMock()
    result = await writer.write_strategy2_decision(
        timestamp=_TS,
        action="ENTER_LONG",
        reason="Type A reversal confirmed",
        state_4h="Coiling",
        cusum_direction="LONG",
        hawkes_lambda=0.5,
        hawkes_threshold=0.3,
        step_reached=6,
    )
    assert result is True
    call_args = writer._conn.execute.call_args[0]
    assert "INSERT INTO v2_decision_trail" in call_args[0]
    assert call_args[2] == "ENTER_LONG"
    assert call_args[3] == "strategy2_evaluate"
    assert call_args[6] == "AUTO"
    assert call_args[7] == "sel_v2_strategy2"


@pytest.mark.asyncio
async def test_write_strategy2_decision_observe():
    writer = DBWriter()
    writer._conn = AsyncMock()
    result = await writer.write_strategy2_decision(
        timestamp=_TS,
        action="OBSERVE",
        reason="Step 1b H1: λ too low",
        state_4h="Drifting_Calm",
        cusum_direction="SHORT",
        hawkes_lambda=0.01,
        hawkes_threshold=0.3,
        step_reached=2,
    )
    assert result is True


@pytest.mark.asyncio
async def test_write_strategy2_decision_graceful_failure():
    writer = DBWriter()
    writer._conn = AsyncMock()
    writer._conn.execute.side_effect = RuntimeError("connection lost")
    result = await writer.write_strategy2_decision(
        _TS, "ABORT", "cascade block", "Cascade", None, None, None, 2
    )
    assert result is False


# ── write_inverse_vocab_event ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_write_inverse_vocab_event_wave5_placeholder():
    writer = DBWriter()
    writer._conn = AsyncMock()
    result = await writer.write_inverse_vocab_event(
        timestamp=_TS,
        vocab="Absorption",
        tool_source="hawkes",
        intensity=1.5,
        associated_state="Critical",
        tool_metadata={"eta": 0.92},
        observation_only=True,
    )
    assert result is True
    call_args = writer._conn.execute.call_args[0]
    assert "INSERT INTO v2_inverse_vocab_events" in call_args[0]
    assert call_args[2] == "Absorption"
    assert call_args[6] == "hawkes"
    assert call_args[7] is True   # observation_only


# ── write_states_bulk ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_write_states_bulk_returns_count():
    writer = DBWriter()
    writer._conn = AsyncMock()
    records = [_make_state_record(state=StateLabel.COILING) for _ in range(5)]
    count = await writer.write_states_bulk(records)
    assert count == 5
    writer._conn.executemany.assert_called_once()
    # self-heal regression guard: must upsert (refresh stale states on recompute), not DO NOTHING
    sql = writer._conn.executemany.call_args[0][0]
    assert "ON CONFLICT (timestamp) DO UPDATE" in sql
    assert "DO NOTHING" not in sql


@pytest.mark.asyncio
async def test_write_states_bulk_no_connection():
    writer = DBWriter()
    records = [_make_state_record()]
    count = await writer.write_states_bulk(records)
    assert count == 0


@pytest.mark.asyncio
async def test_write_states_bulk_graceful_failure():
    writer = DBWriter()
    writer._conn = AsyncMock()
    writer._conn.executemany.side_effect = Exception("bulk insert failed")
    records = [_make_state_record()]
    count = await writer.write_states_bulk(records)
    assert count == 0


# ── clear_state_history ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_clear_state_history():
    writer = DBWriter()
    writer._conn = AsyncMock()
    await writer.clear_state_history()
    writer._conn.execute.assert_called_once_with("TRUNCATE v2_state_history")


@pytest.mark.asyncio
async def test_clear_state_history_no_op_when_not_connected():
    writer = DBWriter()
    # Should not raise
    await writer.clear_state_history()
