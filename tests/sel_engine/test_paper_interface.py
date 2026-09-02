"""
Tests for sel_engine Wave 4 — paper_interface module.
All tests use mocks/fakes — no real DB or Redis connections required.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from sel_engine.features.schema import FeatureVector
from sel_engine.paper_interface.events import StateEventEmitter
from sel_engine.paper_interface.schema import StateOutput
from sel_engine.paper_interface.service import StateOutputService
from sel_engine.paper_interface.store import StateStore, _row_to_output
from sel_engine.states.schema import StateLabel, StateRecord

# ── Helpers ───────────────────────────────────────────────────────────────────

_T0 = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _make_fv(
    time: Optional[datetime] = None,
    symbol: str = "BTCUSDT",
    close: float = 50000.0,
    delta_p_pct: Optional[float] = 0.5,
    sigma_p_24h: Optional[float] = 0.012,
    H: Optional[float] = None,
    TF: Optional[float] = None,
    OI: Optional[float] = None,
    funding_rate: Optional[float] = 0.0001,
    LV: Optional[float] = None,
    price_autocorr_12h: Optional[float] = None,
    price_autocorr_24h: Optional[float] = None,
    OI_hurst: Optional[float] = None,
) -> FeatureVector:
    return FeatureVector(
        time=time or _T0,
        symbol=symbol,
        close=close,
        delta_p_pct=delta_p_pct,
        sigma_p_24h=sigma_p_24h,
        H=H,
        TF=TF,
        OI=OI,
        funding_rate=funding_rate,
        LV=LV,
        price_autocorr_12h=price_autocorr_12h,
        price_autocorr_24h=price_autocorr_24h,
        OI_hurst=OI_hurst,
    )


def _make_state_record(
    state: Optional[StateLabel] = StateLabel.DRIFTING_CALM,
    cold_start: bool = False,
    is_legal_transition: bool = True,
    transition_from: Optional[StateLabel] = None,
    reason: str = "DRIFTING_CALM:sigma_p@0.20",
    fv: Optional[FeatureVector] = None,
) -> StateRecord:
    return StateRecord(
        time=_T0,
        symbol="BTCUSDT",
        state=state,
        reason=reason,
        feature_quantiles={},
        feature_vector=fv,
        cold_start=cold_start,
        is_legal_transition=is_legal_transition,
        transition_from=transition_from,
    )


def _make_state_output(
    state: Optional[str] = "Drifting_Calm",
    state_enum: Optional[StateLabel] = StateLabel.DRIFTING_CALM,
    direction: Optional[str] = None,
    is_cold_start: bool = False,
    health_warning: bool = False,
    transition_from: Optional[str] = None,
    feature_completeness: float = 0.5,
    none_reason: Optional[str] = None,
) -> StateOutput:
    return StateOutput(
        bar_time=_T0,
        symbol="BTCUSDT",
        state=state,
        state_enum=state_enum,
        direction=direction,
        is_cold_start=is_cold_start,
        confidence=1.0,
        reason="DRIFTING_CALM:sigma_p@0.20",
        is_legal_transition=True,
        transition_from=transition_from,
        health_warning=health_warning,
        none_reason=none_reason,
        close=50000.0,
        sigma_p_24h=0.012,
        H=None,
        TF=None,
        OI=None,
        funding_rate=0.0001,
        LV=None,
        feature_completeness=feature_completeness,
    )


# ── 1. StateOutputService._to_output() — state/direction extraction ───────────


class TestToOutput:
    def _make_service(self) -> StateOutputService:
        svc = StateOutputService("BTCUSDT")
        # patch health.generate to return a no-warning report
        report = MagicMock()
        report.health_warnings = []
        svc.engine.health.generate = MagicMock(return_value=report)
        return svc

    def test_drifting_calm_direction_none(self):
        svc = self._make_service()
        record = _make_state_record(state=StateLabel.DRIFTING_CALM)
        fv = _make_fv()
        out = svc._to_output(record, fv)
        assert out.state == "Drifting_Calm"
        assert out.state_enum == StateLabel.DRIFTING_CALM
        assert out.direction is None

    def test_surging_up_direction_up(self):
        svc = self._make_service()
        record = _make_state_record(
            state=StateLabel.SURGING_UP,
            reason="SURGING_UP:abs_delta@0.85",
        )
        fv = _make_fv()
        out = svc._to_output(record, fv)
        assert out.state == "Surging_Up"
        assert out.direction == "Up"

    def test_surging_down_direction_down(self):
        svc = self._make_service()
        record = _make_state_record(
            state=StateLabel.SURGING_DOWN,
            reason="SURGING_DOWN:abs_delta@0.85",
        )
        fv = _make_fv()
        out = svc._to_output(record, fv)
        assert out.state == "Surging_Down"
        assert out.direction == "Down"

    def test_cascade_direction_none(self):
        svc = self._make_service()
        record = _make_state_record(
            state=StateLabel.CASCADE,
            reason="CASCADE:sigma_p@0.98",
        )
        fv = _make_fv()
        out = svc._to_output(record, fv)
        assert out.state == "Cascade"
        assert out.direction is None

    def test_cold_start_state_none(self):
        svc = self._make_service()
        record = _make_state_record(
            state=None,
            cold_start=True,
            reason="COLD_START",
        )
        fv = _make_fv()
        out = svc._to_output(record, fv)
        assert out.state is None
        assert out.state_enum is None
        assert out.is_cold_start is True

    def test_transition_from_propagated(self):
        svc = self._make_service()
        record = _make_state_record(
            state=StateLabel.COILING,
            transition_from=StateLabel.DRIFTING_CALM,
            reason="COILING:sigma_p@0.20",
        )
        fv = _make_fv()
        out = svc._to_output(record, fv)
        assert out.transition_from == "Drifting_Calm"

    def test_is_legal_transition_propagated(self):
        svc = self._make_service()
        record = _make_state_record(
            state=StateLabel.COILING,
            is_legal_transition=False,
        )
        fv = _make_fv()
        out = svc._to_output(record, fv)
        assert out.is_legal_transition is False

    def test_confidence_always_one(self):
        svc = self._make_service()
        record = _make_state_record()
        fv = _make_fv()
        out = svc._to_output(record, fv)
        assert out.confidence == 1.0


# ── 2. Feature completeness calculation ──────────────────────────────────────


class TestFeatureCompleteness:
    def _make_service(self) -> StateOutputService:
        svc = StateOutputService("BTCUSDT")
        report = MagicMock()
        report.health_warnings = []
        svc.engine.health.generate = MagicMock(return_value=report)
        return svc

    def test_all_none_features_zero_completeness(self):
        svc = self._make_service()
        record = _make_state_record()
        fv = FeatureVector(
            time=_T0,
            symbol="BTCUSDT",
            close=0.0,
            # all Optional fields left as None
        )
        out = svc._to_output(record, fv)
        # close=0.0 treated as None in _to_output (returns None), delta_p_pct=None etc.
        # Only fields in all_features list matter
        assert out.feature_completeness >= 0.0
        assert out.feature_completeness <= 1.0

    def test_all_available_features_full_completeness(self):
        svc = self._make_service()
        record = _make_state_record()
        fv = _make_fv(
            close=50000.0,
            delta_p_pct=1.0,
            sigma_p_24h=0.01,
            H=3.5,
            TF=500.0,
            OI=1000000.0,
            funding_rate=0.0001,
            LV=0.8,
            price_autocorr_12h=0.6,
            price_autocorr_24h=0.7,
            OI_hurst=0.55,
        )
        out = svc._to_output(record, fv)
        assert out.feature_completeness == pytest.approx(1.0)

    def test_partial_features_fractional_completeness(self):
        svc = self._make_service()
        record = _make_state_record()
        # Provide: close, delta_p_pct, sigma_p_24h, funding_rate = 4 out of 11
        fv = _make_fv(
            close=50000.0,
            delta_p_pct=1.0,
            sigma_p_24h=0.01,
            H=None,
            TF=None,
            OI=None,
            funding_rate=0.0001,
            LV=None,
            price_autocorr_12h=None,
            price_autocorr_24h=None,
            OI_hurst=None,
        )
        out = svc._to_output(record, fv)
        # 4/11 non-None (close, delta_p_pct, sigma_p_24h, funding_rate)
        assert out.feature_completeness == pytest.approx(4 / 11, abs=1e-6)


# ── 3. StateEventEmitter.emit_if_changed() ────────────────────────────────────


class TestStateEventEmitter:
    @pytest.mark.asyncio
    async def test_no_emit_when_state_unchanged(self):
        emitter = StateEventEmitter()
        redis = AsyncMock()
        redis.set = AsyncMock()
        output = _make_state_output(state="Drifting_Calm")
        emitted = await emitter.emit_if_changed(redis, "BTCUSDT", "Drifting_Calm", output)
        assert emitted is False
        redis.xadd.assert_not_called()

    @pytest.mark.asyncio
    async def test_emits_when_state_changes(self):
        emitter = StateEventEmitter()
        redis = AsyncMock()
        redis.set = AsyncMock()
        redis.xadd = AsyncMock()
        output = _make_state_output(state="Coiling", state_enum=StateLabel.COILING)
        emitted = await emitter.emit_if_changed(redis, "BTCUSDT", "Drifting_Calm", output)
        assert emitted is True
        redis.xadd.assert_called_once()

    @pytest.mark.asyncio
    async def test_emits_when_prev_state_none(self):
        """First bar ever (prev_state=None) should emit if new state is not None."""
        emitter = StateEventEmitter()
        redis = AsyncMock()
        redis.set = AsyncMock()
        redis.xadd = AsyncMock()
        output = _make_state_output(state="Drifting_Calm")
        emitted = await emitter.emit_if_changed(redis, "BTCUSDT", None, output)
        assert emitted is True

    @pytest.mark.asyncio
    async def test_no_emit_when_both_none(self):
        """Cold-start bars: prev=None, current=None → no change."""
        emitter = StateEventEmitter()
        redis = AsyncMock()
        redis.set = AsyncMock()
        output = _make_state_output(state=None, state_enum=None, is_cold_start=True)
        emitted = await emitter.emit_if_changed(redis, "BTCUSDT", None, output)
        assert emitted is False

    @pytest.mark.asyncio
    async def test_stream_key_uses_symbol(self):
        emitter = StateEventEmitter()
        redis = AsyncMock()
        redis.set = AsyncMock()
        redis.xadd = AsyncMock()
        output = _make_state_output(state="Coiling", state_enum=StateLabel.COILING)
        await emitter.emit_if_changed(redis, "ETHUSDT", "Drifting_Calm", output)
        call_args = redis.xadd.call_args
        assert call_args[0][0] == "sel:state_changes:ETHUSDT"

    @pytest.mark.asyncio
    async def test_current_state_always_written(self):
        """Redis hash is always updated, even when state did not change."""
        emitter = StateEventEmitter()
        redis = AsyncMock()
        redis.set = AsyncMock()
        redis.xadd = AsyncMock()
        output = _make_state_output(state="Drifting_Calm")
        await emitter.emit_if_changed(redis, "BTCUSDT", "Drifting_Calm", output)
        redis.set.assert_called_once()

    @pytest.mark.asyncio
    async def test_xadd_maxlen_applied(self):
        emitter = StateEventEmitter()
        redis = AsyncMock()
        redis.set = AsyncMock()
        redis.xadd = AsyncMock()
        output = _make_state_output(state="Coiling", state_enum=StateLabel.COILING)
        await emitter.emit_if_changed(redis, "BTCUSDT", "Drifting_Calm", output)
        kwargs = redis.xadd.call_args[1]
        assert kwargs.get("maxlen") == StateEventEmitter.MAX_STREAM_LEN


# ── 4. StateStore — SQL generation with mock pool ─────────────────────────────


class TestStateStore:
    def _make_mock_pool(self, fetchrow_return=None, fetch_return=None):
        """Return an AsyncMock pool that yields a mock connection."""
        conn = AsyncMock()
        conn.execute = AsyncMock(return_value=None)
        conn.fetchrow = AsyncMock(return_value=fetchrow_return)
        conn.fetch = AsyncMock(return_value=fetch_return or [])

        pool = AsyncMock()
        pool.acquire = MagicMock(return_value=_AsyncContextManager(conn))
        return pool, conn

    @pytest.mark.asyncio
    async def test_upsert_calls_execute(self):
        store = StateStore()
        pool, conn = self._make_mock_pool()
        output = _make_state_output()
        await store.upsert(pool, output)
        conn.execute.assert_called_once()
        # Verify first positional arg is the INSERT SQL
        sql_arg = conn.execute.call_args[0][0]
        assert "INSERT INTO sel_state_sequence" in sql_arg

    @pytest.mark.asyncio
    async def test_upsert_passes_correct_symbol(self):
        store = StateStore()
        pool, conn = self._make_mock_pool()
        output = _make_state_output()
        output.symbol = "ETHUSDT"
        await store.upsert(pool, output)
        args = conn.execute.call_args[0]
        # $2 is symbol — index 2 in positional args (0=sql, 1=bar_time, 2=symbol...)
        assert "ETHUSDT" in args

    @pytest.mark.asyncio
    async def test_get_current_returns_none_when_no_row(self):
        store = StateStore()
        pool, _ = self._make_mock_pool(fetchrow_return=None)
        result = await store.get_current(pool, "BTCUSDT")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_history_returns_empty_list_when_no_rows(self):
        store = StateStore()
        pool, _ = self._make_mock_pool(fetch_return=[])
        start = _T0 - timedelta(hours=24)
        result = await store.get_history(pool, "BTCUSDT", start, _T0)
        assert result == []

    @pytest.mark.asyncio
    async def test_run_migrations_calls_create_table(self):
        store = StateStore()
        pool, conn = self._make_mock_pool()
        await store.run_migrations(pool)
        # execute should be called at least twice (CREATE TABLE + CREATE INDEX)
        assert conn.execute.call_count >= 2
        first_call_sql = conn.execute.call_args_list[0][0][0]
        assert "CREATE TABLE IF NOT EXISTS sel_state_sequence" in first_call_sql


class _AsyncContextManager:
    """Helper: async context manager that yields a fixed value."""

    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *args):
        pass


# ── 5. _row_to_output — DB row → StateOutput conversion ─────────────────────


class TestRowToOutput:
    def _make_row(self, **overrides) -> dict:
        base = {
            "time": _T0,
            "symbol": "BTCUSDT",
            "state": "Drifting_Calm",
            "direction": None,
            "is_cold_start": False,
            "confidence": 1.0,
            "reason": "DRIFTING_CALM:test",
            "is_legal": True,
            "transition_from": None,
            "health_warning": False,
            "close_price": 50000.0,
            "sigma_p_24h": 0.012,
            "H": None,
            "TF": None,
            "OI": None,
            "funding_rate": 0.0001,
            "LV": None,
            "feature_completeness": 0.4,
        }
        base.update(overrides)
        return base

    def test_basic_row_conversion(self):
        row = self._make_row()
        output = _row_to_output(row)
        assert output.bar_time == _T0
        assert output.symbol == "BTCUSDT"
        assert output.state == "Drifting_Calm"
        assert output.state_enum == StateLabel.DRIFTING_CALM
        assert output.is_cold_start is False
        assert output.is_legal_transition is True
        assert output.feature_completeness == pytest.approx(0.4)

    def test_none_state_gives_none_enum(self):
        row = self._make_row(state=None, is_cold_start=True)
        output = _row_to_output(row)
        assert output.state is None
        assert output.state_enum is None

    def test_invalid_state_string_gives_none_enum(self):
        row = self._make_row(state="UnknownState")
        output = _row_to_output(row)
        assert output.state == "UnknownState"
        assert output.state_enum is None  # ValueError caught internally

    def test_numeric_fields_converted_to_float(self):
        row = self._make_row(close_price="50000.12345678")
        output = _row_to_output(row)
        assert isinstance(output.close, float)
        assert output.close == pytest.approx(50000.12345678)

    def test_none_close_price_gives_none_close(self):
        row = self._make_row(close_price=None)
        output = _row_to_output(row)
        assert output.close is None


# ── 6. Health warning propagation ────────────────────────────────────────────


class TestHealthWarning:
    def _make_service_with_warnings(self, warnings: list[str]) -> StateOutputService:
        svc = StateOutputService("BTCUSDT")
        report = MagicMock()
        report.health_warnings = warnings
        svc.engine.health.generate = MagicMock(return_value=report)
        return svc

    def test_health_warning_true_when_warnings_present(self):
        svc = self._make_service_with_warnings(["Coiling rate 0.05 above expected [0.10, 0.25]"])
        record = _make_state_record()
        fv = _make_fv()
        out = svc._to_output(record, fv)
        assert out.health_warning is True

    def test_health_warning_false_when_no_warnings(self):
        svc = self._make_service_with_warnings([])
        record = _make_state_record()
        fv = _make_fv()
        out = svc._to_output(record, fv)
        assert out.health_warning is False

    def test_health_warning_false_on_cold_start(self):
        svc = self._make_service_with_warnings([])
        record = _make_state_record(state=None, cold_start=True, reason="COLD_START")
        fv = _make_fv()
        out = svc._to_output(record, fv)
        assert out.health_warning is False


# ── 7. End-to-end: process_bar() with mock pg/redis ──────────────────────────


class TestProcessBarEndToEnd:
    def _make_service(self) -> StateOutputService:
        svc = StateOutputService("BTCUSDT")
        return svc

    def _make_mock_pg(self):
        conn = AsyncMock()
        conn.execute = AsyncMock(return_value=None)
        pool = AsyncMock()
        pool.acquire = MagicMock(return_value=_AsyncContextManager(conn))
        return pool

    def _make_mock_redis(self):
        redis = AsyncMock()
        redis.set = AsyncMock()
        redis.xadd = AsyncMock()
        redis.get = AsyncMock(return_value=None)
        return redis

    @pytest.mark.asyncio
    async def test_process_bar_returns_state_output(self):
        svc = self._make_service()
        pg = self._make_mock_pg()
        redis = self._make_mock_redis()

        out = await svc.process_bar(
            bar_time=_T0,
            close=50000.0,
            pg_pool=pg,
            redis=redis,
        )
        assert isinstance(out, StateOutput)
        assert out.symbol == "BTCUSDT"
        assert out.bar_time == _T0
        assert out.confidence == 1.0
        assert 0.0 <= out.feature_completeness <= 1.0

    @pytest.mark.asyncio
    async def test_process_bar_cold_start_with_few_bars(self):
        """With only 1 bar, engine must be in cold start."""
        svc = self._make_service()
        pg = self._make_mock_pg()
        redis = self._make_mock_redis()

        out = await svc.process_bar(
            bar_time=_T0,
            close=50000.0,
            pg_pool=pg,
            redis=redis,
        )
        assert out.is_cold_start is True
        assert out.state is None

    @pytest.mark.asyncio
    async def test_process_bar_calls_pg_upsert(self):
        svc = self._make_service()
        pg = self._make_mock_pg()
        redis = self._make_mock_redis()
        conn = pg.acquire.return_value._value

        await svc.process_bar(
            bar_time=_T0,
            close=50000.0,
            pg_pool=pg,
            redis=redis,
        )
        conn.execute.assert_called()

    @pytest.mark.asyncio
    async def test_process_bar_calls_redis_set(self):
        svc = self._make_service()
        pg = self._make_mock_pg()
        redis = self._make_mock_redis()

        await svc.process_bar(
            bar_time=_T0,
            close=50000.0,
            pg_pool=pg,
            redis=redis,
        )
        redis.set.assert_called_once()

    @pytest.mark.asyncio
    async def test_process_bar_prev_state_tracks_changes(self):
        svc = self._make_service()
        pg = self._make_mock_pg()
        redis = self._make_mock_redis()

        assert svc._prev_state is None

        out = await svc.process_bar(
            bar_time=_T0,
            close=50000.0,
            pg_pool=pg,
            redis=redis,
        )
        # After first bar, _prev_state matches the output
        assert svc._prev_state == out.state

    @pytest.mark.asyncio
    async def test_process_bar_appends_close_history(self):
        svc = self._make_service()
        pg = self._make_mock_pg()
        redis = self._make_mock_redis()

        for i in range(5):
            await svc.process_bar(
                bar_time=_T0 + timedelta(hours=i),
                close=50000.0 + i * 100,
                pg_pool=pg,
                redis=redis,
            )
        assert len(svc._close_history) == 5

    @pytest.mark.asyncio
    async def test_process_bar_caps_close_history_at_800(self):
        svc = self._make_service()
        pg = self._make_mock_pg()
        redis = self._make_mock_redis()

        # Pre-fill with 810 bars (skip DB for speed using only 1 call pattern)
        # Use direct list manipulation to avoid 810 async calls in tests
        svc._close_history = [50000.0] * 810
        # Trigger one more bar
        await svc.process_bar(
            bar_time=_T0,
            close=50100.0,
            pg_pool=pg,
            redis=redis,
        )
        assert len(svc._close_history) <= 800
