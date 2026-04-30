"""FastAPI router for sel state endpoints (Wave 4).

Mount into an existing gateway:
    from sel_engine.paper_interface.api import router as sel_router
    app.include_router(sel_router)

Endpoints read from TimescaleDB / Redis — they do NOT call the in-memory
StateEngine directly, so they work across process boundaries.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from fastapi import APIRouter, Depends, HTTPException, Query
    _FASTAPI_AVAILABLE = True
except ImportError:
    _FASTAPI_AVAILABLE = False
    APIRouter = None  # type: ignore

if _FASTAPI_AVAILABLE:
    router = APIRouter(prefix="/sel", tags=["sel-state"])

    # ------------------------------------------------------------------
    # Dependency helpers
    # ------------------------------------------------------------------

    async def _get_pg_pool():
        """Yield a connection pool.  Override by providing pool at app startup."""
        import asyncpg
        url = os.environ.get(
            "TIMESCALE_URL",
            "postgresql://cw4:changeme@timescaledb:5432/cw4",
        )
        # asyncpg doesn't understand SQLAlchemy's postgresql+asyncpg:// scheme
        url = url.replace("postgresql+asyncpg://", "postgresql://")
        pool = await asyncpg.create_pool(url, min_size=1, max_size=3)
        try:
            yield pool
        finally:
            await pool.close()

    async def _get_redis():
        """Yield a Redis client.  Override by providing client at app startup."""
        try:
            import redis.asyncio as aioredis
        except ImportError:
            import aioredis  # type: ignore
        url = os.environ.get("REDIS_URL", "redis://helios-redis:6379/0")
        client = await aioredis.from_url(url, decode_responses=True)
        try:
            yield client
        finally:
            await client.aclose()

    # ------------------------------------------------------------------
    # Endpoints
    # ------------------------------------------------------------------

    @router.get("/state/current")
    async def get_current_state(
        symbol: str = Query(default="BTCUSDT", description="Trading pair symbol"),
        redis=Depends(_get_redis),
    ) -> dict:
        """
        Returns the current (latest) sel state for a symbol.
        Reads from Redis hash — fastest path, no DB round-trip.
        Falls back to DB if key not present.
        """
        from .events import _CURRENT_STATE_KEY_PREFIX
        key = f"{_CURRENT_STATE_KEY_PREFIX}:{symbol}"
        raw = await redis.get(key)
        if raw is not None:
            return json.loads(raw)

        # Fallback: DB query
        from .store import StateStore
        import asyncpg
        url = os.environ.get(
            "TIMESCALE_URL",
            "postgresql://cw4:changeme@timescaledb:5432/cw4",
        ).replace("postgresql+asyncpg://", "postgresql://")
        try:
            pool = await asyncpg.create_pool(url, min_size=1, max_size=1)
            store = StateStore()
            output = await store.get_current(pool, symbol)
            await pool.close()
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"DB unavailable: {exc}") from exc

        if output is None:
            raise HTTPException(status_code=404, detail=f"No state found for {symbol}")
        return {
            "bar_time": output.bar_time.isoformat(),
            "symbol": output.symbol,
            "state": output.state,
            "direction": output.direction,
            "is_cold_start": output.is_cold_start,
            "confidence": output.confidence,
            "reason": output.reason,
            "is_legal_transition": output.is_legal_transition,
            "transition_from": output.transition_from,
            "health_warning": output.health_warning,
            "feature_completeness": output.feature_completeness,
        }

    @router.get("/state/history")
    async def get_state_history(
        symbol: str = Query(default="BTCUSDT"),
        hours: int = Query(default=168, ge=1, le=8760),
        limit: int = Query(default=200, ge=1, le=1000),
    ) -> list:
        """Returns state history for the past N hours."""
        from .store import StateStore
        import asyncpg
        url = os.environ.get(
            "TIMESCALE_URL",
            "postgresql://cw4:changeme@timescaledb:5432/cw4",
        ).replace("postgresql+asyncpg://", "postgresql://")
        now = datetime.now(tz=timezone.utc)
        start = now - timedelta(hours=hours)
        try:
            pool = await asyncpg.create_pool(url, min_size=1, max_size=1)
            store = StateStore()
            records = await store.get_history(pool, symbol, start, now, limit=limit)
            await pool.close()
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"DB unavailable: {exc}") from exc

        return [
            {
                "bar_time": r.bar_time.isoformat(),
                "symbol": r.symbol,
                "state": r.state,
                "direction": r.direction,
                "is_cold_start": r.is_cold_start,
                "confidence": r.confidence,
                "reason": r.reason,
                "is_legal_transition": r.is_legal_transition,
                "transition_from": r.transition_from,
                "health_warning": r.health_warning,
                "feature_completeness": r.feature_completeness,
            }
            for r in records
        ]

    @router.get("/state/health")
    async def get_health_report(
        symbol: str = Query(default="BTCUSDT"),
    ) -> dict:
        """
        Returns the current HealthReport for symbol.
        Note: this report is generated from DB-persisted records, not the in-memory engine.
        For a richer report, callers should maintain their own StateEngine instance.
        """
        from .store import StateStore
        import asyncpg
        url = os.environ.get(
            "TIMESCALE_URL",
            "postgresql://cw4:changeme@timescaledb:5432/cw4",
        ).replace("postgresql+asyncpg://", "postgresql://")
        now = datetime.now(tz=timezone.utc)
        start = now - timedelta(hours=720)
        try:
            pool = await asyncpg.create_pool(url, min_size=1, max_size=1)
            store = StateStore()
            records = await store.get_history(pool, symbol, start, now, limit=1000)
            await pool.close()
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"DB unavailable: {exc}") from exc

        total = len(records)
        cold_count = sum(1 for r in records if r.is_cold_start)
        active = total - cold_count
        state_counts: dict[str, int] = {}
        health_warnings = []
        for r in records:
            if not r.is_cold_start and r.state:
                state_counts[r.state] = state_counts.get(r.state, 0) + 1
            if r.health_warning:
                health_warnings.append(f"{r.bar_time.isoformat()}: health_warning=True")

        return {
            "symbol": symbol,
            "period_start": start.isoformat(),
            "period_end": now.isoformat(),
            "total_bars": total,
            "cold_start_bars": cold_count,
            "active_bars": active,
            "state_counts": state_counts,
            "health_warning_count": len(health_warnings),
        }

    @router.get("/state/events/stream")
    async def stream_state_events(
        symbol: str = Query(default="BTCUSDT"),
        last_id: str = Query(default="0"),
        count: int = Query(default=100, ge=1, le=1000),
        redis=Depends(_get_redis),
    ) -> dict:
        """Returns events from the Redis stream since last_id."""
        from .events import StateEventEmitter
        stream_key = f"{StateEventEmitter.STREAM_KEY_PREFIX}:{symbol}"
        try:
            raw_entries = await redis.xread(
                {stream_key: last_id},
                count=count,
            )
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"Redis unavailable: {exc}") from exc

        events = []
        new_last_id = last_id
        if raw_entries:
            for _stream_name, entries in raw_entries:
                for entry_id, fields in entries:
                    events.append({"id": entry_id, **fields})
                    new_last_id = entry_id

        return {
            "symbol": symbol,
            "last_id": new_last_id,
            "count": len(events),
            "events": events,
        }

else:
    # FastAPI not installed — expose a sentinel so imports don't crash
    router = None  # type: ignore
    logger.warning("FastAPI not installed; sel paper_interface API router is disabled.")
