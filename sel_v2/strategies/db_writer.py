"""
DB writer for Wave 2 → Wave 3 persistence layer.

Responsibilities:
  - v2_cusum_events:   every CUSUM-Short trigger from Strategy 2
  - v2_decision_trail: every evaluate() call result (ENTER_LONG / ABORT / OBSERVE …)
  - v2_state_history:  every 4H bar state record from the state machine
  - v2_inverse_vocab_events: log-only tools (Wave 5 placeholder interface)

Design contract:
  - evaluate() in strategy2_entry.py remains a PURE function returning EntryDecision
  - This writer is called EXTERNALLY by the event loop / replay
  - Write failures log a warning and do NOT raise (never block decision path)
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import asyncpg

from sel_v2.states.schema import StateRecord

logger = logging.getLogger(__name__)


def _default_db_url() -> str:
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("POSTGRES_PORT", "5434")
    user = os.getenv("POSTGRES_USER", "selene_app")
    password = os.getenv("POSTGRES_PASSWORD", "2eb0eac6f7a01f5c41de2dacffa928474fb575c884365ac5")
    dbname = os.getenv("POSTGRES_DB", "selene")
    return f"postgresql://{user}:{password}@{host}:{port}/{dbname}"


class DBWriter:
    """
    Async DB writer backed by a single asyncpg connection.

    Usage:
        writer = DBWriter(db_url=...)
        await writer.connect()
        await writer.write_state(record)
        await writer.close()

    For the offline replay, a single connection is reused throughout.
    """

    def __init__(self, db_url: Optional[str] = None) -> None:
        self._url = db_url or _default_db_url()
        self._conn: Optional[asyncpg.Connection] = None

    async def connect(self) -> None:
        self._conn = await asyncpg.connect(self._url)
        logger.info("DBWriter: connected to %s", self._url.split("@")[-1])

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    # ── v2_state_history ───────────────────────────────────────────────────────

    async def write_state(self, record: StateRecord) -> bool:
        """
        Insert one row into v2_state_history.
        Returns True on success, False if write failed (non-blocking).
        """
        if self._conn is None:
            logger.warning("DBWriter.write_state: not connected, skipping")
            return False
        try:
            await self._conn.execute(
                """
                INSERT INTO v2_state_history
                    (timestamp, state, sub_state, state_features,
                     transition_from, transition_via, duration_4h)
                VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7)
                ON CONFLICT DO NOTHING
                """,
                record.timestamp,
                record.state.value,
                record.sub_state,
                json.dumps(record.state_features),
                record.transition_from.value if record.transition_from else None,
                record.transition_via,
                record.duration_4h,
            )
            return True
        except Exception as exc:
            logger.warning("DBWriter.write_state failed: %s", exc)
            return False

    # ── v2_cusum_events ────────────────────────────────────────────────────────

    async def write_cusum_event(
        self,
        timestamp: datetime,
        cusum_type: str,          # 'short'
        direction: str,           # 'up' / 'down'
        peak_value: float,
        threshold_h: float,
        z_returns_window: Optional[list] = None,
    ) -> bool:
        """Write a CUSUM trigger event. Call whenever CUSUM-Short triggers."""
        if self._conn is None:
            logger.warning("DBWriter.write_cusum_event: not connected, skipping")
            return False
        try:
            await self._conn.execute(
                """
                INSERT INTO v2_cusum_events
                    (timestamp, cusum_type, direction, peak_value,
                     threshold_h, z_returns_window)
                VALUES ($1, $2, $3, $4, $5, $6::jsonb)
                """,
                timestamp,
                cusum_type,
                direction,
                peak_value,
                threshold_h,
                json.dumps(z_returns_window) if z_returns_window is not None else None,
            )
            return True
        except Exception as exc:
            logger.warning("DBWriter.write_cusum_event failed: %s", exc)
            return False

    # ── v2_decision_trail ──────────────────────────────────────────────────────

    async def write_strategy2_decision(
        self,
        timestamp: datetime,
        action: str,                  # ENTER_LONG / ENTER_SHORT / ABORT / OBSERVE
        reason: str,
        state_4h: Optional[str],
        cusum_direction: Optional[str],
        hawkes_lambda: Optional[float],
        hawkes_threshold: Optional[float],
        step_reached: int,
        metadata: Optional[dict] = None,
    ) -> bool:
        """
        Write Strategy 2 evaluate() result to v2_decision_trail.

        Maps fields into the governance trail schema:
          decision_type     → action (ENTER_LONG etc.)
          trigger_source    → 'strategy2_evaluate'
          target_component  → 'strategy2_entry'
          decision_basis    → reason string
          wiki_decision     → 'AUTO' (machine decision)
          created_by        → 'sel_v2_strategy2'
        """
        if self._conn is None:
            logger.warning("DBWriter.write_strategy2_decision: not connected, skipping")
            return False
        try:
            extra = {
                "state_4h": state_4h,
                "cusum_direction": cusum_direction,
                "hawkes_lambda": hawkes_lambda,
                "hawkes_threshold": hawkes_threshold,
                "step_reached": step_reached,
            }
            if metadata:
                extra.update(metadata)

            await self._conn.execute(
                """
                INSERT INTO v2_decision_trail
                    (timestamp, decision_type, trigger_source, target_component,
                     decision_basis, wiki_decision, created_by, tool_evaluation)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
                """,
                timestamp,
                action,
                "strategy2_evaluate",
                "strategy2_entry",
                reason,
                "AUTO",
                "sel_v2_strategy2",
                json.dumps(extra),
            )
            return True
        except Exception as exc:
            logger.warning("DBWriter.write_strategy2_decision failed: %s", exc)
            return False

    # ── v2_inverse_vocab_events ────────────────────────────────────────────────

    async def write_inverse_vocab_event(
        self,
        timestamp: datetime,
        vocab: str,
        tool_source: str,
        intensity: Optional[float] = None,
        associated_state: Optional[str] = None,
        tool_metadata: Optional[dict] = None,
        observation_only: bool = True,
    ) -> bool:
        """
        Write a log-only (observation) tool event.
        Used for W2/B1/I1 etc. (Wave 5 tools) — Wave 3 interface placeholder.
        """
        if self._conn is None:
            logger.warning("DBWriter.write_inverse_vocab_event: not connected, skipping")
            return False
        try:
            await self._conn.execute(
                """
                INSERT INTO v2_inverse_vocab_events
                    (timestamp, vocab, intensity, associated_state,
                     triggered_decision, tool_source, observation_only, tool_metadata)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
                """,
                timestamp,
                vocab,
                intensity,
                associated_state,
                False,
                tool_source,
                observation_only,
                json.dumps(tool_metadata) if tool_metadata else None,
            )
            return True
        except Exception as exc:
            logger.warning("DBWriter.write_inverse_vocab_event failed: %s", exc)
            return False

    # ── Bulk helpers ───────────────────────────────────────────────────────────

    async def write_states_bulk(self, records: list[StateRecord]) -> int:
        """
        Bulk-insert state records using executemany.
        Returns number of successfully written records.
        """
        if self._conn is None:
            logger.warning("DBWriter.write_states_bulk: not connected")
            return 0

        rows = []
        for r in records:
            rows.append((
                r.timestamp,
                r.state.value,
                r.sub_state,
                json.dumps(r.state_features),
                r.transition_from.value if r.transition_from else None,
                r.transition_via,
                r.duration_4h,
            ))

        try:
            await self._conn.executemany(
                """
                INSERT INTO v2_state_history
                    (timestamp, state, sub_state, state_features,
                     transition_from, transition_via, duration_4h)
                VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7)
                ON CONFLICT DO NOTHING
                """,
                rows,
            )
            return len(rows)
        except Exception as exc:
            logger.warning("DBWriter.write_states_bulk failed: %s", exc)
            return 0

    async def clear_state_history(self) -> None:
        """Truncate v2_state_history — used by replay --reset flag."""
        if self._conn is None:
            return
        await self._conn.execute("TRUNCATE v2_state_history")
        logger.info("DBWriter: v2_state_history truncated")

    # ── v2_trades (Wave 4) ─────────────────────────────────────────────────────

    async def write_trade_entry(
        self,
        trade_id: str,
        strategy: str,
        sub_account: str,
        entry_time: datetime,
        entry_price: float,
        direction: str,
        size: float,
        leverage: float,
        instrument: str,
        entry_state: str,
        entry_cusum_id: Optional[str] = None,
        entry_vocab: Optional[list] = None,
        entry_confidence: Optional[float] = None,
    ) -> bool:
        """Insert a new open trade into v2_trades."""
        if self._conn is None:
            logger.warning("DBWriter.write_trade_entry: not connected, skipping")
            return False
        try:
            await self._conn.execute(
                """
                INSERT INTO v2_trades
                    (id, strategy, sub_account, entry_time, entry_price,
                     direction, size, leverage, instrument, entry_state,
                     entry_cusum_id, entry_vocab, entry_confidence)
                VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                        $11::uuid, $12::jsonb, $13)
                """,
                trade_id,
                strategy,
                sub_account,
                entry_time,
                entry_price,
                direction,
                size,
                leverage,
                instrument,
                entry_state,
                entry_cusum_id,
                json.dumps(entry_vocab) if entry_vocab is not None else None,
                entry_confidence,
            )
            return True
        except Exception as exc:
            logger.warning("DBWriter.write_trade_entry failed: %s", exc)
            return False

    async def upsert_trade(
        self,
        *,
        trade_id: str,
        strategy: str,
        sub_account: str,
        entry_time: datetime,
        entry_price: float,
        direction: str,
        size: float,
        leverage: float,
        instrument: str,
        entry_state: str,
        exit_time: Optional[datetime] = None,
        exit_price: Optional[float] = None,
        exit_reason: Optional[str] = None,
        pnl_usdt: Optional[float] = None,
        pnl_pct: Optional[float] = None,
        fees_paid: float = 0.0,
    ) -> bool:
        """Idempotent insert-or-update of a v2_trades row keyed by a deterministic id.

        The paper engine replays the full bar history every tick, so the same
        logical trade is re-emitted each cycle; a deterministic ``trade_id`` plus
        ON CONFLICT DO UPDATE makes persistence idempotent (open trades upsert with
        NULL exits; a later cycle fills the exit fields when the position closes)."""
        if self._conn is None:
            logger.warning("DBWriter.upsert_trade: not connected, skipping")
            return False
        try:
            await self._conn.execute(
                """
                INSERT INTO v2_trades
                    (id, strategy, sub_account, entry_time, entry_price,
                     direction, size, leverage, instrument, entry_state,
                     exit_time, exit_price, exit_reason, pnl_usdt, pnl_pct, fees_paid)
                VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                        $11, $12, $13, $14, $15, $16)
                ON CONFLICT (id) DO UPDATE SET
                    exit_time = EXCLUDED.exit_time,
                    exit_price = EXCLUDED.exit_price,
                    exit_reason = EXCLUDED.exit_reason,
                    pnl_usdt = EXCLUDED.pnl_usdt,
                    pnl_pct = EXCLUDED.pnl_pct,
                    fees_paid = EXCLUDED.fees_paid
                """,
                trade_id, strategy, sub_account, entry_time, entry_price,
                direction, size, leverage, instrument, entry_state,
                exit_time, exit_price, exit_reason, pnl_usdt, pnl_pct, fees_paid,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("DBWriter.upsert_trade failed: %s", exc)
            return False

    async def write_trade_exit(
        self,
        trade_id: str,
        exit_time: datetime,
        exit_price: float,
        exit_reason: str,
        pnl_usdt: float,
        pnl_pct: float,
        fees_paid: float = 0.0,
    ) -> bool:
        """Update an existing v2_trades row with exit details."""
        if self._conn is None:
            logger.warning("DBWriter.write_trade_exit: not connected, skipping")
            return False
        try:
            await self._conn.execute(
                """
                UPDATE v2_trades
                SET exit_time=$2, exit_price=$3, exit_reason=$4,
                    pnl_usdt=$5, pnl_pct=$6, fees_paid=$7
                WHERE id=$1::uuid
                """,
                trade_id,
                exit_time,
                exit_price,
                exit_reason,
                pnl_usdt,
                pnl_pct,
                fees_paid,
            )
            return True
        except Exception as exc:
            logger.warning("DBWriter.write_trade_exit failed: %s", exc)
            return False

    # ── v2_strategy_phase_history (Wave 4) ─────────────────────────────────────

    async def write_phase_history(
        self,
        timestamp: datetime,
        strategy: str,
        from_phase: Optional[str],
        to_phase: str,
        rolling_w: Optional[float] = None,
        rolling_r: Optional[float] = None,
        sample_size: Optional[int] = None,
        kelly_fraction_estimated: Optional[float] = None,
        kelly_cap_lower: Optional[float] = None,
        kelly_cap_upper: Optional[float] = None,
        decision_id: Optional[str] = None,
    ) -> bool:
        """Record a Kelly phase transition or diagnostic snapshot."""
        if self._conn is None:
            logger.warning("DBWriter.write_phase_history: not connected, skipping")
            return False
        try:
            await self._conn.execute(
                """
                INSERT INTO v2_strategy_phase_history
                    (timestamp, strategy, from_phase, to_phase,
                     rolling_w, rolling_r, sample_size, kelly_fraction_estimated,
                     kelly_cap_lower, kelly_cap_upper, decision_id)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::uuid)
                """,
                timestamp,
                strategy,
                from_phase,
                to_phase,
                rolling_w,
                rolling_r,
                sample_size,
                kelly_fraction_estimated,
                kelly_cap_lower,
                kelly_cap_upper,
                decision_id,
            )
            return True
        except Exception as exc:
            logger.warning("DBWriter.write_phase_history failed: %s", exc)
            return False

    # ── v2_decision_trail — Strategy 1 (Wave 4) ────────────────────────────────

    async def write_strategy1_decision(
        self,
        timestamp: datetime,
        action: str,
        reason: str,
        state_4h: Optional[str],
        direction: Optional[str],
        cusum_positive: float,
        cusum_threshold: float,
        step_reached: int,
        funding_pctile: Optional[float] = None,
        oi_direction_against: bool = False,
        metadata: Optional[dict] = None,
    ) -> bool:
        """Write Strategy 1 evaluate() result to v2_decision_trail."""
        if self._conn is None:
            logger.warning("DBWriter.write_strategy1_decision: not connected, skipping")
            return False
        try:
            extra = {
                "state_4h": state_4h,
                "direction": direction,
                "cusum_positive": cusum_positive,
                "cusum_threshold": cusum_threshold,
                "step_reached": step_reached,
                "funding_pctile": funding_pctile,
                "oi_direction_against": oi_direction_against,
            }
            if metadata:
                extra.update(metadata)

            await self._conn.execute(
                """
                INSERT INTO v2_decision_trail
                    (timestamp, decision_type, trigger_source, target_component,
                     decision_basis, wiki_decision, created_by, tool_evaluation)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
                """,
                timestamp,
                action,
                "strategy1_evaluate",
                "strategy1_entry",
                reason,
                "AUTO",
                "sel_v2_strategy1",
                json.dumps(extra),
            )
            return True
        except Exception as exc:
            logger.warning("DBWriter.write_strategy1_decision failed: %s", exc)
            return False
