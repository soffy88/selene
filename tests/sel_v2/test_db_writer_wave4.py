"""
Tests for sel_v2.strategies.db_writer — Wave 4 methods.

Covers:
- write_trade_entry: INSERT into v2_trades with correct params
- write_trade_exit: UPDATE v2_trades row with exit details
- write_phase_history: INSERT into v2_strategy_phase_history
- write_strategy1_decision: INSERT into v2_decision_trail (strategy1 source)
- Graceful failure (DB error → False, never raises)
- No-connection guard (returns False immediately)
"""
import json
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from sel_v2.strategies.db_writer import DBWriter

_TS = datetime(2025, 6, 1, 12, 0, tzinfo=timezone.utc)
_TRADE_ID = "550e8400-e29b-41d4-a716-446655440000"


# ── write_trade_entry ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_write_trade_entry_inserts_correct_params():
    writer = DBWriter()
    writer._conn = AsyncMock()

    result = await writer.write_trade_entry(
        trade_id=_TRADE_ID,
        strategy="strategy_1",
        sub_account="sub_1",
        entry_time=_TS,
        entry_price=50_000.0,
        direction="LONG",
        size=16_000.0,
        leverage=1.0,
        instrument="BTC-USDT-SWAP",
        entry_state="Coiling",
        entry_cusum_id=None,
        entry_vocab=["Sweep"],
        entry_confidence=0.85,
    )

    assert result is True
    writer._conn.execute.assert_called_once()
    call_args = writer._conn.execute.call_args[0]
    assert "INSERT INTO v2_trades" in call_args[0]
    assert call_args[1] == _TRADE_ID
    assert call_args[2] == "strategy_1"
    assert call_args[3] == "sub_1"
    assert call_args[4] == _TS
    assert call_args[5] == 50_000.0
    assert call_args[6] == "LONG"
    assert call_args[7] == 16_000.0
    assert call_args[8] == 1.0
    assert call_args[9] == "BTC-USDT-SWAP"
    assert call_args[10] == "Coiling"


@pytest.mark.asyncio
async def test_write_trade_entry_serializes_vocab():
    writer = DBWriter()
    writer._conn = AsyncMock()

    await writer.write_trade_entry(
        trade_id=_TRADE_ID,
        strategy="strategy_1",
        sub_account="sub_1",
        entry_time=_TS,
        entry_price=50_000.0,
        direction="LONG",
        size=16_000.0,
        leverage=1.0,
        instrument="BTC-USDT-SWAP",
        entry_state="Coiling",
        entry_vocab=["Sweep", "Saturation"],
    )
    call_args = writer._conn.execute.call_args[0]
    # entry_vocab is serialized as JSON string
    assert call_args[12] == json.dumps(["Sweep", "Saturation"])


@pytest.mark.asyncio
async def test_write_trade_entry_none_vocab():
    writer = DBWriter()
    writer._conn = AsyncMock()

    await writer.write_trade_entry(
        trade_id=_TRADE_ID,
        strategy="strategy_2",
        sub_account="sub_2",
        entry_time=_TS,
        entry_price=50_000.0,
        direction="SHORT",
        size=4_000.0,
        leverage=1.0,
        instrument="BTC-USDT-SWAP",
        entry_state="Surging",
        entry_vocab=None,
    )
    call_args = writer._conn.execute.call_args[0]
    assert call_args[12] is None  # null when no vocab


@pytest.mark.asyncio
async def test_write_trade_entry_no_connection():
    writer = DBWriter()
    result = await writer.write_trade_entry(
        trade_id=_TRADE_ID,
        strategy="strategy_1",
        sub_account="sub_1",
        entry_time=_TS,
        entry_price=50_000.0,
        direction="LONG",
        size=16_000.0,
        leverage=1.0,
        instrument="BTC-USDT-SWAP",
        entry_state="Coiling",
    )
    assert result is False


@pytest.mark.asyncio
async def test_write_trade_entry_graceful_failure():
    writer = DBWriter()
    writer._conn = AsyncMock()
    writer._conn.execute.side_effect = Exception("deadlock")

    result = await writer.write_trade_entry(
        trade_id=_TRADE_ID,
        strategy="strategy_1",
        sub_account="sub_1",
        entry_time=_TS,
        entry_price=50_000.0,
        direction="LONG",
        size=16_000.0,
        leverage=1.0,
        instrument="BTC-USDT-SWAP",
        entry_state="Coiling",
    )
    assert result is False


# ── write_trade_exit ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_write_trade_exit_updates_correct_columns():
    writer = DBWriter()
    writer._conn = AsyncMock()

    result = await writer.write_trade_exit(
        trade_id=_TRADE_ID,
        exit_time=_TS,
        exit_price=55_000.0,
        exit_reason="Drawdown stop",
        pnl_usdt=1_600.0,
        pnl_pct=0.10,
        fees_paid=0.0,
    )

    assert result is True
    writer._conn.execute.assert_called_once()
    call_args = writer._conn.execute.call_args[0]
    assert "UPDATE v2_trades" in call_args[0]
    assert call_args[1] == _TRADE_ID
    assert call_args[2] == _TS
    assert call_args[3] == 55_000.0
    assert call_args[4] == "Drawdown stop"
    assert call_args[5] == pytest.approx(1_600.0)
    assert call_args[6] == pytest.approx(0.10)
    assert call_args[7] == 0.0


@pytest.mark.asyncio
async def test_write_trade_exit_no_connection():
    writer = DBWriter()
    result = await writer.write_trade_exit(
        trade_id=_TRADE_ID,
        exit_time=_TS,
        exit_price=48_000.0,
        exit_reason="cascade",
        pnl_usdt=-800.0,
        pnl_pct=-0.05,
    )
    assert result is False


@pytest.mark.asyncio
async def test_write_trade_exit_graceful_failure():
    writer = DBWriter()
    writer._conn = AsyncMock()
    writer._conn.execute.side_effect = RuntimeError("connection reset")

    result = await writer.write_trade_exit(
        trade_id=_TRADE_ID,
        exit_time=_TS,
        exit_price=48_000.0,
        exit_reason="cascade",
        pnl_usdt=-800.0,
        pnl_pct=-0.05,
    )
    assert result is False


# ── write_phase_history ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_write_phase_history_full_params():
    writer = DBWriter()
    writer._conn = AsyncMock()

    result = await writer.write_phase_history(
        timestamp=_TS,
        strategy="strategy_1",
        from_phase="PHASE_1",
        to_phase="PHASE_2",
        rolling_w=0.65,
        rolling_r=2.5,
        sample_size=35,
        kelly_fraction_estimated=0.46,
        kelly_cap_lower=0.05,
        kelly_cap_upper=0.25,
        decision_id=None,
    )

    assert result is True
    writer._conn.execute.assert_called_once()
    call_args = writer._conn.execute.call_args[0]
    assert "INSERT INTO v2_strategy_phase_history" in call_args[0]
    assert call_args[1] == _TS
    assert call_args[2] == "strategy_1"
    assert call_args[3] == "PHASE_1"
    assert call_args[4] == "PHASE_2"
    assert call_args[5] == pytest.approx(0.65)
    assert call_args[6] == pytest.approx(2.5)
    assert call_args[7] == 35
    assert call_args[11] is None  # decision_id


@pytest.mark.asyncio
async def test_write_phase_history_initial_phase0():
    """Phase 0 entry: from_phase=None, to_phase=PHASE_0."""
    writer = DBWriter()
    writer._conn = AsyncMock()

    result = await writer.write_phase_history(
        timestamp=_TS,
        strategy="strategy_2",
        from_phase=None,
        to_phase="PHASE_0",
    )

    assert result is True
    call_args = writer._conn.execute.call_args[0]
    assert call_args[3] is None   # from_phase
    assert call_args[4] == "PHASE_0"


@pytest.mark.asyncio
async def test_write_phase_history_no_connection():
    writer = DBWriter()
    result = await writer.write_phase_history(
        timestamp=_TS, strategy="strategy_1",
        from_phase=None, to_phase="PHASE_0",
    )
    assert result is False


@pytest.mark.asyncio
async def test_write_phase_history_graceful_failure():
    writer = DBWriter()
    writer._conn = AsyncMock()
    writer._conn.execute.side_effect = Exception("unique violation")

    result = await writer.write_phase_history(
        timestamp=_TS, strategy="strategy_1",
        from_phase="PHASE_0", to_phase="PHASE_1",
    )
    assert result is False


# ── write_strategy1_decision ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_write_strategy1_decision_enter_long():
    writer = DBWriter()
    writer._conn = AsyncMock()

    result = await writer.write_strategy1_decision(
        timestamp=_TS,
        action="ENTER_LONG",
        reason="Step 8: sized entry",
        state_4h="Coiling",
        direction="LONG",
        cusum_positive=3.5,
        cusum_threshold=2.0,
        step_reached=8,
        funding_pctile=0.45,
        oi_direction_against=False,
    )

    assert result is True
    writer._conn.execute.assert_called_once()
    call_args = writer._conn.execute.call_args[0]
    assert "INSERT INTO v2_decision_trail" in call_args[0]
    assert call_args[2] == "ENTER_LONG"
    assert call_args[3] == "strategy1_evaluate"
    assert call_args[4] == "strategy1_entry"
    assert call_args[5] == "Step 8: sized entry"
    assert call_args[6] == "AUTO"
    assert call_args[7] == "sel_v2_strategy1"


@pytest.mark.asyncio
async def test_write_strategy1_decision_tool_evaluation_payload():
    writer = DBWriter()
    writer._conn = AsyncMock()

    await writer.write_strategy1_decision(
        timestamp=_TS,
        action="ABORT",
        reason="Step 4a: funding extreme",
        state_4h="Coiling",
        direction="LONG",
        cusum_positive=3.5,
        cusum_threshold=2.0,
        step_reached=4,
        funding_pctile=0.96,
        oi_direction_against=False,
    )
    call_args = writer._conn.execute.call_args[0]
    # tool_evaluation is last positional param (index 8), serialized JSON
    payload = json.loads(call_args[8])
    assert payload["state_4h"] == "Coiling"
    assert payload["direction"] == "LONG"
    assert payload["step_reached"] == 4
    assert payload["funding_pctile"] == pytest.approx(0.96)
    assert payload["oi_direction_against"] is False


@pytest.mark.asyncio
async def test_write_strategy1_decision_observe():
    writer = DBWriter()
    writer._conn = AsyncMock()

    result = await writer.write_strategy1_decision(
        timestamp=_TS,
        action="OBSERVE",
        reason="Step 2: min-dwell not met",
        state_4h="Coiling",
        direction=None,
        cusum_positive=0.0,
        cusum_threshold=2.0,
        step_reached=2,
    )
    assert result is True


@pytest.mark.asyncio
async def test_write_strategy1_decision_no_connection():
    writer = DBWriter()
    result = await writer.write_strategy1_decision(
        timestamp=_TS,
        action="OBSERVE",
        reason="not connected",
        state_4h="Coiling",
        direction=None,
        cusum_positive=0.0,
        cusum_threshold=2.0,
        step_reached=1,
    )
    assert result is False


@pytest.mark.asyncio
async def test_write_strategy1_decision_graceful_failure():
    writer = DBWriter()
    writer._conn = AsyncMock()
    writer._conn.execute.side_effect = Exception("timeout")

    result = await writer.write_strategy1_decision(
        timestamp=_TS,
        action="ENTER_SHORT",
        reason="Step 8: sized entry",
        state_4h="Drifting-Charged",
        direction="SHORT",
        cusum_positive=0.0,
        cusum_threshold=2.0,
        step_reached=8,
    )
    assert result is False


@pytest.mark.asyncio
async def test_write_strategy1_decision_metadata_merged():
    """Extra metadata dict is merged into tool_evaluation JSON."""
    writer = DBWriter()
    writer._conn = AsyncMock()

    await writer.write_strategy1_decision(
        timestamp=_TS,
        action="ENTER_LONG",
        reason="Step 8",
        state_4h="Coiling",
        direction="LONG",
        cusum_positive=3.5,
        cusum_threshold=2.0,
        step_reached=8,
        metadata={"vocab_modifier": 0.2, "base_size_pct": 0.18},
    )
    call_args = writer._conn.execute.call_args[0]
    payload = json.loads(call_args[8])
    assert payload["vocab_modifier"] == pytest.approx(0.2)
    assert payload["base_size_pct"] == pytest.approx(0.18)
