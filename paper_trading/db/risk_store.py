"""RiskStore — persist RiskGate state (Redis) and risk events (TimescaleDB)."""

from __future__ import annotations

import json
import logging
from typing import Optional

from paper_trading.risk import RiskEvent

logger = logging.getLogger(__name__)

RISK_STATE_REDIS_KEY = "sel:risk_state:{symbol}"
RISK_EVENTS_TABLE = "sel_risk_events"

_MIGRATION_SQL = f"""
CREATE TABLE IF NOT EXISTS {RISK_EVENTS_TABLE} (
    time         TIMESTAMPTZ  NOT NULL,
    symbol       VARCHAR(20)  NOT NULL,
    rule         VARCHAR(50)  NOT NULL,
    details      TEXT,
    action_taken VARCHAR(20)
);
SELECT create_hypertable(
    '{RISK_EVENTS_TABLE}', 'time', if_not_exists => TRUE
);
CREATE INDEX IF NOT EXISTS sel_risk_events_symbol_time_idx
    ON {RISK_EVENTS_TABLE} (symbol, time DESC);
"""

_INSERT_EVENT_SQL = f"""
    INSERT INTO {RISK_EVENTS_TABLE}
        (time, symbol, rule, details, action_taken)
    VALUES ($1, $2, $3, $4, $5)
"""


class RiskStore:
    """Thin async persistence layer for ``RiskGate``.

    All methods are static so callers can use the class without instantiation,
    matching the style of ``PaperTradingStore``.
    """

    # ------------------------------------------------------------------
    # Redis — gate state (survives process restarts)
    # ------------------------------------------------------------------

    @staticmethod
    async def save_state(redis, symbol: str, state_dict: dict) -> None:
        """Persist ``RiskGate.to_state_dict()`` to Redis."""
        key = RISK_STATE_REDIS_KEY.format(symbol=symbol)
        await redis.set(key, json.dumps(state_dict))
        logger.debug("RiskStore: saved state for %s", symbol)

    @staticmethod
    async def load_state(redis, symbol: str) -> Optional[dict]:
        """Load previously persisted gate state from Redis.

        Returns ``None`` if no state exists (first run).
        """
        key = RISK_STATE_REDIS_KEY.format(symbol=symbol)
        raw = await redis.get(key)
        if raw is None:
            return None
        return json.loads(raw)

    # ------------------------------------------------------------------
    # TimescaleDB — risk event log
    # ------------------------------------------------------------------

    @staticmethod
    async def insert_event(pool, event: RiskEvent) -> None:
        """Append a single ``RiskEvent`` row to ``sel_risk_events``."""
        async with pool.acquire() as conn:
            await conn.execute(
                _INSERT_EVENT_SQL,
                event.time,
                event.symbol,
                event.rule,
                event.details,
                event.action_taken,
            )

    @staticmethod
    async def run_migrations(pool) -> None:
        """Create ``sel_risk_events`` hypertable if it doesn't exist (idempotent)."""
        async with pool.acquire() as conn:
            await conn.execute(_MIGRATION_SQL)
        logger.info("RiskStore: migrations applied")
