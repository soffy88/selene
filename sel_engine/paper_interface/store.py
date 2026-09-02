"""StateStore: persists sel state sequence to TimescaleDB (Wave 4)."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from sel_engine.states.schema import StateLabel

from .schema import StateOutput

logger = logging.getLogger(__name__)

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS sel_state_sequence (
    time            TIMESTAMPTZ NOT NULL,
    symbol          VARCHAR(20) NOT NULL,
    state           VARCHAR(30),
    direction       VARCHAR(10),
    is_cold_start   BOOLEAN NOT NULL DEFAULT FALSE,
    confidence      NUMERIC(4,3) DEFAULT 1.0,
    reason          TEXT,
    is_legal        BOOLEAN,
    transition_from VARCHAR(30),
    health_warning  BOOLEAN DEFAULT FALSE,
    close_price     NUMERIC(20,8),
    sigma_p_24h     NUMERIC(10,8),
    H               NUMERIC(10,8),
    TF              NUMERIC(20,8),
    OI              NUMERIC(20,8),
    funding_rate    NUMERIC(10,8),
    LV              NUMERIC(6,4),
    feature_completeness NUMERIC(4,3),
    computed_at     TIMESTAMPTZ DEFAULT NOW()
);
"""

_CREATE_HYPERTABLE_SQL = """
SELECT create_hypertable('sel_state_sequence', 'time', if_not_exists => TRUE);
"""

_CREATE_INDEX_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS sel_state_seq_time_sym
    ON sel_state_sequence(time, symbol);
"""

_UPSERT_SQL = """
INSERT INTO sel_state_sequence (
    time, symbol, state, none_reason, reason,
    cold_start, is_legal_transition, transition_from, feature_quantiles
) VALUES (
    $1, $2, $3, $4, $5,
    $6, $7, $8, $9::jsonb
)
ON CONFLICT (time, symbol)
DO UPDATE SET
    state               = EXCLUDED.state,
    none_reason         = EXCLUDED.none_reason,
    reason              = EXCLUDED.reason,
    cold_start          = EXCLUDED.cold_start,
    is_legal_transition = EXCLUDED.is_legal_transition,
    transition_from     = EXCLUDED.transition_from,
    feature_quantiles   = EXCLUDED.feature_quantiles
;
"""

_GET_CURRENT_SQL = """
SELECT
    ss.time,
    ss.symbol,
    ss.state,
    NULL                          AS direction,
    ss.cold_start                 AS is_cold_start,
    1.0                           AS confidence,
    ss.reason,
    ss.is_legal_transition        AS is_legal,
    ss.transition_from,
    FALSE                         AS health_warning,
    sf.close                      AS close_price,
    sf.sigma_p_24h,
    sf.h                          AS "H",
    sf.tf                         AS "TF",
    sf.oi                         AS "OI",
    sf.funding_rate,
    sf.lv                         AS "LV",
    (
        CASE WHEN sf.sigma_p_24h  IS NOT NULL THEN 1 ELSE 0 END +
        CASE WHEN sf.h            IS NOT NULL THEN 1 ELSE 0 END +
        CASE WHEN sf.tf           IS NOT NULL THEN 1 ELSE 0 END +
        CASE WHEN sf.oi           IS NOT NULL THEN 1 ELSE 0 END +
        CASE WHEN sf.funding_rate IS NOT NULL THEN 1 ELSE 0 END +
        CASE WHEN sf.lv           IS NOT NULL THEN 1 ELSE 0 END
    )::float / 6.0                AS feature_completeness
FROM sel_state_sequence ss
LEFT JOIN sel_features sf ON ss.time = sf.time AND ss.symbol = sf.symbol
WHERE ss.symbol = $1
  AND ss.cold_start = FALSE
  AND ss.state IS NOT NULL
ORDER BY ss.time DESC
LIMIT 1;
"""

_GET_HISTORY_SQL = """
SELECT
    ss.time,
    ss.symbol,
    ss.state,
    NULL                          AS direction,
    ss.cold_start                 AS is_cold_start,
    1.0                           AS confidence,
    ss.reason,
    ss.is_legal_transition        AS is_legal,
    ss.transition_from,
    FALSE                         AS health_warning,
    sf.close                      AS close_price,
    sf.sigma_p_24h,
    sf.h                          AS "H",
    sf.tf                         AS "TF",
    sf.oi                         AS "OI",
    sf.funding_rate,
    sf.lv                         AS "LV",
    (
        CASE WHEN sf.sigma_p_24h  IS NOT NULL THEN 1 ELSE 0 END +
        CASE WHEN sf.h            IS NOT NULL THEN 1 ELSE 0 END +
        CASE WHEN sf.tf           IS NOT NULL THEN 1 ELSE 0 END +
        CASE WHEN sf.oi           IS NOT NULL THEN 1 ELSE 0 END +
        CASE WHEN sf.funding_rate IS NOT NULL THEN 1 ELSE 0 END +
        CASE WHEN sf.lv           IS NOT NULL THEN 1 ELSE 0 END
    )::float / 6.0                AS feature_completeness
FROM sel_state_sequence ss
LEFT JOIN sel_features sf ON ss.time = sf.time AND ss.symbol = sf.symbol
WHERE ss.symbol = $1
  AND ss.time >= $2
  AND ss.time <= $3
ORDER BY ss.time ASC
LIMIT $4;
"""


def _row_to_output(row: dict) -> StateOutput:
    """Convert a DB row dict to a StateOutput dataclass."""
    state_str = row.get("state")
    state_enum: Optional[StateLabel] = None
    if state_str is not None:
        try:
            state_enum = StateLabel(state_str)
        except ValueError:
            pass

    return StateOutput(
        bar_time=row["time"],
        symbol=row["symbol"],
        state=state_str,
        state_enum=state_enum,
        direction=row.get("direction"),
        is_cold_start=bool(row.get("is_cold_start", False)),
        confidence=float(row.get("confidence") or 1.0),
        reason=row.get("reason") or "",
        is_legal_transition=bool(row.get("is_legal", True)),
        transition_from=row.get("transition_from"),
        health_warning=bool(row.get("health_warning", False)),
        close=float(row["close_price"]) if row.get("close_price") is not None else None,
        sigma_p_24h=float(row["sigma_p_24h"]) if row.get("sigma_p_24h") is not None else None,
        H=float(row["H"]) if row.get("H") is not None else None,
        TF=float(row["TF"]) if row.get("TF") is not None else None,
        OI=float(row["OI"]) if row.get("OI") is not None else None,
        funding_rate=float(row["funding_rate"]) if row.get("funding_rate") is not None else None,
        LV=float(row["LV"]) if row.get("LV") is not None else None,
        feature_completeness=float(row.get("feature_completeness") or 0.0),
    )


class StateStore:
    """Persists and retrieves StateOutput records from TimescaleDB."""

    async def run_migrations(self, pg_pool) -> None:
        """Create sel_state_sequence hypertable if it does not exist."""
        async with pg_pool.acquire() as conn:
            await conn.execute(_CREATE_TABLE_SQL)
            try:
                await conn.execute(_CREATE_HYPERTABLE_SQL)
            except Exception as exc:
                # Already a hypertable or TimescaleDB not installed — not fatal
                logger.debug("create_hypertable skipped: %s", exc)
            await conn.execute(_CREATE_INDEX_SQL)
        logger.info("sel_state_sequence migrations complete")

    async def upsert(self, pg_pool, output: StateOutput) -> None:
        """Upsert one StateOutput row."""
        async with pg_pool.acquire() as conn:
            await conn.execute(
                _UPSERT_SQL,
                output.bar_time,
                output.symbol,
                output.state,
                output.none_reason,
                output.reason,
                output.is_cold_start,
                output.is_legal_transition,
                output.transition_from,
                "{}",
            )

    async def get_current(self, pg_pool, symbol: str) -> Optional[StateOutput]:
        """Return the latest non-cold-start state for symbol, or None."""
        async with pg_pool.acquire() as conn:
            row = await conn.fetchrow(_GET_CURRENT_SQL, symbol)
        if row is None:
            return None
        return _row_to_output(dict(row))

    async def get_history(
        self,
        pg_pool,
        symbol: str,
        start: datetime,
        end: datetime,
        limit: int = 200,
    ) -> list[StateOutput]:
        """Return state records in [start, end] ascending, up to limit rows."""
        async with pg_pool.acquire() as conn:
            rows = await conn.fetch(_GET_HISTORY_SQL, symbol, start, end, limit)
        return [_row_to_output(dict(r)) for r in rows]
