"""
Unit tests for BarCloseRunner.

Tests use mocks for DB pool and Redis; they do NOT hit real infrastructure.
Integration tests (requiring a real DB + Redis) are skipped in CI.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sel_engine.states.schema import StateNoneReason

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_pool(existing_bar: bool = False) -> MagicMock:
    """Return a mock asyncpg pool. existing_bar=True simulates idempotency check hit."""
    pool = MagicMock()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=MagicMock() if existing_bar else None)
    conn.fetch = AsyncMock(return_value=[])
    pool.acquire = MagicMock(
        return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=conn),
            __aexit__=AsyncMock(return_value=False),
        )
    )
    return pool, conn


def _make_redis(h_samples=None, depth_samples=None, tf_accum=None) -> MagicMock:
    redis = AsyncMock()
    redis.lrange = AsyncMock(return_value=h_samples or [])
    if depth_samples is not None:
        redis.lrange = AsyncMock(
            side_effect=lambda key, *a: (
                [json.dumps(s).encode() for s in depth_samples] if "depth" in key else (h_samples or [])
            )
        )
    redis.get = AsyncMock(return_value=str(tf_accum).encode() if tf_accum else None)
    redis.set = AsyncMock()
    redis.incr = AsyncMock()
    return redis


BAR_TIME = datetime(2026, 4, 28, 13, 0, 0, tzinfo=timezone.utc)

# ── Test 1: missing collector data → MISSING_DATA ────────────────────────────


@pytest.mark.asyncio
async def test_scheduler_handles_missing_data():
    """
    When all WIKI_REQUIRED collector data is absent (H=None, TF=None, OI=None),
    the recognizer short-circuits with none_reason=MISSING_DATA.
    The scheduler must write the state record (state=None, none_reason=MISSING_DATA).
    """
    pool, conn = _make_pool(existing_bar=False)
    redis = _make_redis()  # no samples → all None

    # Provide minimal closes so data_lag gate passes
    conn.fetch = AsyncMock(return_value=[])  # TF_history, sigma, H, OI all empty
    conn.fetchrow = AsyncMock(return_value=None)  # not already processed

    written_state_records = []

    async def fake_write_state(p, record):
        written_state_records.append(record)

    async def fake_write_fv(p, fv):
        pass

    async def fake_read_closes(*a, **kw):
        return [95_000.0, 94_500.0, 95_200.0]  # minimal 3 bars

    async def fake_read_sigma(*a, **kw):
        return []

    async def fake_read_h(*a, **kw):
        return []

    async def fake_read_oi(*a, **kw):
        return []

    async def fake_read_depth(*a, **kw):
        return None, None

    with (
        patch("sel_engine.scheduler.bar_close_runner.read_closes", fake_read_closes),
        patch("sel_engine.scheduler.bar_close_runner.read_sigma_p_history", fake_read_sigma),
        patch("sel_engine.scheduler.bar_close_runner.read_H_history", fake_read_h),
        patch("sel_engine.scheduler.bar_close_runner.read_OI_history", fake_read_oi),
        patch("sel_engine.scheduler.bar_close_runner.read_depth_24h_means", fake_read_depth),
        patch("sel_engine.scheduler.bar_close_runner.write_feature_vector", fake_write_fv),
        patch("sel_engine.scheduler.bar_close_runner.write_state_record", fake_write_state),
    ):
        from sel_engine.scheduler.bar_close_runner import BarCloseRunner

        runner = BarCloseRunner(symbol="BTCUSDT", pool=pool, redis=redis)
        await runner.run_bar(BAR_TIME)

    assert len(written_state_records) == 1
    rec = written_state_records[0]
    assert rec.none_reason in (
        StateNoneReason.MISSING_DATA,
        StateNoneReason.COLD_START,
        StateNoneReason.NO_MATCH,
    ), f"unexpected none_reason={rec.none_reason}"
    assert rec.state is None or rec.none_reason == StateNoneReason.COLD_START


# ── Test 2: idempotency ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_scheduler_idempotent_on_same_bar():
    """
    Running run_bar() for the same bar_open_time twice must not result in
    a second write call — the idempotency check must prevent it.
    """
    pool, conn = _make_pool(existing_bar=True)  # simulate bar already in DB
    redis = _make_redis()

    written_calls = []

    async def fake_write_state(p, record):
        written_calls.append(record)

    with patch("sel_engine.scheduler.bar_close_runner.write_state_record", fake_write_state):
        from sel_engine.scheduler.bar_close_runner import BarCloseRunner

        runner = BarCloseRunner(symbol="BTCUSDT", pool=pool, redis=redis)
        await runner.run_bar(BAR_TIME)

    # No write should have been attempted — idempotency check returns early.
    assert len(written_calls) == 0


# ── Test 3: skip incomplete bar (data_lag) ───────────────────────────────────


@pytest.mark.asyncio
async def test_scheduler_skips_incomplete_bar():
    """
    When read_closes returns [] (bar candle not yet in DB — data_lag),
    the scheduler must skip processing without writing anything.
    """
    pool, conn = _make_pool(existing_bar=False)
    redis = _make_redis()

    written_calls = []

    async def fake_write_state(p, record):
        written_calls.append(record)

    async def fake_read_closes(*a, **kw):
        return []  # simulate data_lag: candle not available

    with (
        patch("sel_engine.scheduler.bar_close_runner.read_closes", fake_read_closes),
        patch("sel_engine.scheduler.bar_close_runner.write_state_record", fake_write_state),
    ):
        from sel_engine.scheduler.bar_close_runner import BarCloseRunner

        runner = BarCloseRunner(symbol="BTCUSDT", pool=pool, redis=redis)
        await runner.run_bar(BAR_TIME)

    assert len(written_calls) == 0


# ── Test 4: Postgres column case-folding — reader must use lowercase keys ─────


@pytest.mark.asyncio
async def test_read_H_history_uses_lowercase_key():
    """
    Postgres folds unquoted H to h. read_H_history must access r['h'], not r['H'].
    Regression guard for the KeyError: 'H' bug (Task 2.2.3).
    """
    from sel_engine.db.reader import read_H_history

    pool = MagicMock()
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[{"h": 0.73}])
    pool.acquire = MagicMock(
        return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=conn),
            __aexit__=AsyncMock(return_value=False),
        )
    )

    result = await read_H_history(pool, "BTCUSDT", datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc), limit=1)
    assert result == [0.73], f"Expected [0.73], got {result}"


@pytest.mark.asyncio
async def test_read_tf_history_uses_lowercase_key():
    """
    Postgres folds unquoted TF to tf. _read_tf_history must access r['tf'], not r['TF'].
    Regression guard for same column-case bug (Task 2.2.3).
    """
    pool = MagicMock()
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[{"tf": 123456789.0}])
    pool.acquire = MagicMock(
        return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=conn),
            __aexit__=AsyncMock(return_value=False),
        )
    )

    from sel_engine.scheduler.bar_close_runner import BarCloseRunner

    runner = BarCloseRunner(symbol="BTCUSDT", pool=pool, redis=AsyncMock())
    result = await runner._read_tf_history(datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc))
    assert result == [123456789.0], f"Expected [123456789.0], got {result}"
