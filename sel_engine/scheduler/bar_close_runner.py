"""
Bar-close runner: processes one 1H bar at a time through the sel_engine pipeline.

Pipeline per bar:
  raw data (DB + Redis)
  → FeatureCalculator.compute()
  → depth feature supplement (from orderbook samples)
  → LV recompute
  → StateEngine.process()
  → write sel_features + sel_state_sequence
  → update Redis health keys

The StateEngine is a persistent object across bars; its internal dwell/cooling
state is lost on container restart (see EC-10 in engineering_concerns.md).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import asyncpg
import redis.asyncio as aioredis

from sel_engine.db.reader import (
    read_closes,
    read_depth_24h_means,
    read_H_history,
    read_OI_history,
    read_sigma_p_history,
)
from sel_engine.db.writer import write_feature_vector, write_state_record
from sel_engine.features.calculator import FeatureCalculator
from sel_engine.features.derived import compute_LV
from sel_engine.states.engine import StateEngine

logger = logging.getLogger(__name__)

_REDIS_KEY_LAST_RUN = "sel:scheduler:last_run"
_REDIS_KEY_LAST_BAR = "sel:scheduler:last_bar_processed"
_REDIS_KEY_FAILURES = "sel:scheduler:consecutive_failures"

_CHECK_PROCESSED_SQL = """
    SELECT 1 FROM sel_state_sequence
    WHERE symbol = $1 AND time = $2
    LIMIT 1
"""

_READ_TF_HISTORY_SQL = """
    SELECT TF FROM sel_features
    WHERE symbol = $1 AND time < $2 AND TF IS NOT NULL
    ORDER BY time DESC
    LIMIT 24
"""


class BarCloseRunner:
    """Processes one 1H bar at a time. Idempotent: skips already-written bars."""

    def __init__(
        self,
        symbol: str,
        pool: asyncpg.Pool,
        redis: aioredis.Redis,
    ) -> None:
        self.symbol = symbol
        self.pool = pool
        self.redis = redis
        self.engine = StateEngine(symbol)
        self.calculator = FeatureCalculator()

    async def run_bar(self, bar_open_time: datetime) -> None:
        """
        Process the bar whose open_time == bar_open_time.
        bar_open_time must be a UTC-aware datetime at a full hour boundary.
        """
        bar_ts_iso = bar_open_time.isoformat()
        bar_close_time = bar_open_time + timedelta(hours=1)

        if await self._already_processed(bar_open_time):
            logger.info("bar %s already in sel_state_sequence, skipping", bar_ts_iso)
            return

        # Data availability gate: K-line must exist for this bar.
        closes = await read_closes(self.pool, self.symbol, bar_open_time, limit=50)
        if not closes:
            logger.warning(
                "data_lag: no candle row for bar %s — skipping (will retry next trigger)",
                bar_ts_iso,
            )
            return

        try:
            # ── Read historical context ───────────────────────────────────────
            sigma_p_history = await read_sigma_p_history(
                self.pool, self.symbol, bar_open_time, limit=50
            )
            H_history = await read_H_history(
                self.pool, self.symbol, bar_open_time, limit=50
            )
            # OI: read up to bar_close_time so the most-recent 5-min snapshot is included
            OI_history = await read_OI_history(
                self.pool, self.symbol, bar_close_time, limit=50
            )
            OI_current: Optional[float] = OI_history[-1] if OI_history else None
            total_depth_24h_mean, spread_bps_24h_mean = await read_depth_24h_means(
                self.pool, self.symbol, bar_open_time
            )
            TF_history = await self._read_tf_history(bar_open_time)

            # ── Read intra-bar samples from Redis ────────────────────────────
            H_samples = await self._read_h_samples(bar_ts_iso)
            depth_samples = await self._read_depth_samples(bar_ts_iso)

            # ── Feature computation ──────────────────────────────────────────
            fv = await self.calculator.compute(
                bar_time=bar_open_time,
                symbol=self.symbol,
                closes=closes,
                sigma_p_history=sigma_p_history,
                H_history=H_history,
                OI_history=OI_history,
                OI_current=OI_current,
                total_depth_24h_mean=total_depth_24h_mean,
                spread_bps_24h_mean=spread_bps_24h_mean,
                bids=None,   # raw L2 not available at bar close
                asks=None,
                H_samples=H_samples,
                redis=self.redis,
                bar_ts_iso=bar_ts_iso,
                TF_history=TF_history,
            )

            # Supplement depth features from bar's aggregated orderbook samples.
            # FeatureCalculator uses bids/asks (raw L2) for depth; since those
            # are unavailable at bar close, we override from Redis samples here.
            self._apply_depth_samples(fv, depth_samples)

            # Recompute LV now that depth features may have been supplemented.
            if all(v is not None for v in (
                fv.total_depth, total_depth_24h_mean,
                fv.spread_bps, spread_bps_24h_mean,
            )):
                fv.LV = compute_LV(
                    fv.total_depth, total_depth_24h_mean,
                    fv.spread_bps, spread_bps_24h_mean,
                )
                fv.availability.LV = fv.LV is not None

            # ── State engine ─────────────────────────────────────────────────
            record = self.engine.process(fv)

            # ── Persist ──────────────────────────────────────────────────────
            await write_feature_vector(self.pool, fv)
            await write_state_record(self.pool, record)

            # ── Health metrics ────────────────────────────────────────────────
            now_iso = datetime.now(timezone.utc).isoformat()
            await self.redis.set(_REDIS_KEY_LAST_RUN, now_iso)
            await self.redis.set(_REDIS_KEY_LAST_BAR, bar_ts_iso)
            await self.redis.set(_REDIS_KEY_FAILURES, 0)

            logger.info(
                "bar %s processed: state=%s cold_start=%s none_reason=%s",
                bar_ts_iso,
                record.state.value if record.state else "None",
                record.cold_start,
                record.none_reason.value,
            )

        except Exception as exc:
            logger.error(
                "bar %s processing failed: %s", bar_ts_iso, exc, exc_info=True
            )
            # Failure: do NOT write any partial row to sel_state_sequence.
            # Increment failure counter for monitoring.
            await self.redis.incr(_REDIS_KEY_FAILURES)

    # ── Private helpers ────────────────────────────────────────────────────────

    async def _already_processed(self, bar_open_time: datetime) -> bool:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                _CHECK_PROCESSED_SQL, self.symbol, bar_open_time
            )
        return row is not None

    async def _read_tf_history(self, bar_open_time: datetime) -> Optional[list]:
        """Return last ≤24 TF values from sel_features, oldest-first. None if unavailable."""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(_READ_TF_HISTORY_SQL, self.symbol, bar_open_time)
        if not rows:
            return None
        return [float(r["TF"]) for r in reversed(rows)]

    async def _read_h_samples(self, bar_ts_iso: str) -> Optional[list[float]]:
        """Read intra-bar H samples from Redis list written by orderbook_collector."""
        key = f"sel:h_samples:{self.symbol}:{bar_ts_iso}"
        try:
            raw = await self.redis.lrange(key, 0, -1)
            if not raw:
                return None
            return [float(v) for v in raw]
        except Exception as exc:
            logger.warning("H_samples Redis read failed for %s: %s", bar_ts_iso, exc)
            return None

    async def _read_depth_samples(self, bar_ts_iso: str) -> list[dict]:
        """Read intra-bar depth samples from Redis list written by orderbook_collector."""
        key = f"sel:depth_samples:{self.symbol}:{bar_ts_iso}"
        try:
            raw = await self.redis.lrange(key, 0, -1)
            if not raw:
                return []
            result = []
            for item in raw:
                try:
                    result.append(json.loads(item))
                except (ValueError, TypeError):
                    pass
            return result
        except Exception as exc:
            logger.warning("depth_samples Redis read failed for %s: %s", bar_ts_iso, exc)
            return []

    def _apply_depth_samples(self, fv, samples: list[dict]) -> None:
        """
        Override fv depth features using bar's aggregated orderbook samples.
        FeatureCalculator sets these to None when bids/asks are None (our case at bar close).
        This supplements with per-sample averages from the orderbook_collector.
        """
        if not samples:
            return

        bids = [s["top_5_bid"] for s in samples if s.get("top_5_bid") is not None]
        asks = [s["top_5_ask"] for s in samples if s.get("top_5_ask") is not None]
        spreads = [s["spread_bps"] for s in samples if s.get("spread_bps") is not None]

        if bids:
            fv.top_5_bid_size = sum(bids) / len(bids)
            fv.availability.top_5_bid_size = True
        if asks:
            fv.top_5_ask_size = sum(asks) / len(asks)
            fv.availability.top_5_ask_size = True
        if fv.top_5_bid_size is not None and fv.top_5_ask_size is not None:
            fv.total_depth = fv.top_5_bid_size + fv.top_5_ask_size
            fv.availability.total_depth = True
        if spreads:
            fv.spread_bps = sum(spreads) / len(spreads)
            fv.availability.spread_bps = True
