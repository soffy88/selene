"""TrailStore — TimescaleDB persistence for DecisionTrail records."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Optional

from paper_trading.trail import DecisionTrail

logger = logging.getLogger(__name__)

_TABLE = "sel_decision_trail"

_MIGRATION_SQL = f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    time               TIMESTAMPTZ NOT NULL,
    symbol             VARCHAR(20)  NOT NULL,
    config_hash        VARCHAR(16)  NOT NULL,
    using_claude_default BOOLEAN    NOT NULL DEFAULT TRUE,
    current_state      VARCHAR(30),
    previous_state     VARCHAR(30),
    state_history      JSONB,
    state_reason       TEXT,
    close              NUMERIC(20,8),
    sigma_p_24h        NUMERIC(10,8),
    H                  NUMERIC(10,8),
    TF                 NUMERIC(20,8),
    OI                 NUMERIC(20,8),
    funding_rate       NUMERIC(10,8),
    LV                 NUMERIC(6,4),
    absorption_ratio   NUMERIC(20,8),
    OI_hurst           NUMERIC(6,4),
    feature_completeness NUMERIC(4,3),
    nav_usdt           NUMERIC(20,8),
    position_side      VARCHAR(10),
    position_size_usdt NUMERIC(20,8),
    unrealized_pnl     NUMERIC(20,8),
    realized_pnl       NUMERIC(20,8),
    proposed_action    VARCHAR(20),
    final_action       VARCHAR(20),
    rule_id            VARCHAR(80),
    risk_triggered     VARCHAR(50),
    risk_details       TEXT,
    fill_price         NUMERIC(20,8),
    fill_size_usdt     NUMERIC(20,8),
    fill_fee_usdt      NUMERIC(20,8),
    realized_pnl_bar   NUMERIC(20,8),
    risk_check_passed  BOOLEAN
);
SELECT create_hypertable('{_TABLE}', 'time', if_not_exists => TRUE);
CREATE UNIQUE INDEX IF NOT EXISTS sel_dt_time_sym ON {_TABLE}(time, symbol);
SELECT add_compression_policy('{_TABLE}', INTERVAL '7 days', if_not_exists => TRUE);
"""

_INSERT_SQL = f"""
    INSERT INTO {_TABLE} (
        time, symbol, config_hash, using_claude_default,
        current_state, previous_state, state_history, state_reason,
        close, sigma_p_24h, H, TF, OI, funding_rate, LV,
        absorption_ratio, OI_hurst, feature_completeness,
        nav_usdt, position_side, position_size_usdt,
        unrealized_pnl, realized_pnl,
        proposed_action, final_action, rule_id,
        risk_triggered, risk_details,
        fill_price, fill_size_usdt, fill_fee_usdt, realized_pnl_bar,
        risk_check_passed
    ) VALUES (
        $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,
        $16,$17,$18,$19,$20,$21,$22,$23,$24,$25,$26,$27,$28,
        $29,$30,$31,$32,$33
    )
    ON CONFLICT (time, symbol) DO NOTHING
"""

_QUERY_RANGE_SQL = f"""
    SELECT * FROM {_TABLE}
    WHERE symbol = $1 AND time >= $2 AND time < $3
    ORDER BY time ASC
"""


class TrailStore:
    """Async persistence layer for DecisionTrail."""

    # ------------------------------------------------------------------
    @staticmethod
    async def run_migrations(pool) -> None:
        """Execute DDL — idempotent, safe to call every startup."""
        async with pool.acquire() as conn:
            await conn.execute(_MIGRATION_SQL)
        logger.info("TrailStore: migrations applied")

    # ------------------------------------------------------------------
    @staticmethod
    async def insert(pool, trail: DecisionTrail) -> None:
        """Insert one DecisionTrail row (ignores duplicate time+symbol)."""
        async with pool.acquire() as conn:
            await conn.execute(
                _INSERT_SQL,
                trail.time,
                trail.symbol,
                trail.config_hash,
                trail.using_claude_default,
                trail.current_state,
                trail.previous_state,
                json.dumps(trail.state_history),
                trail.state_reason,
                trail.close,
                trail.sigma_p_24h,
                trail.H,
                trail.TF,
                trail.OI,
                trail.funding_rate,
                trail.LV,
                trail.absorption_ratio,
                trail.OI_hurst,
                trail.feature_completeness,
                trail.nav_usdt,
                trail.position_side,
                trail.position_size_usdt,
                trail.unrealized_pnl_usdt,
                trail.realized_pnl_usdt,
                trail.proposed_action,
                trail.final_action,
                trail.rule_id,
                trail.risk_triggered,
                trail.risk_details,
                trail.fill_price,
                trail.fill_size_usdt,
                trail.fill_fee_usdt,
                trail.realized_pnl_this_bar,
                trail.risk_check_passed,
            )

    # ------------------------------------------------------------------
    @staticmethod
    async def query_range(
        pool,
        symbol: str,
        start: datetime,
        end: datetime,
    ) -> list[DecisionTrail]:
        """Return trails for *symbol* in [start, end)."""
        async with pool.acquire() as conn:
            rows = await conn.fetch(_QUERY_RANGE_SQL, symbol, start, end)

        result: list[DecisionTrail] = []
        for row in rows:
            state_history = json.loads(row["state_history"]) if row["state_history"] else []
            trail = DecisionTrail(
                time=row["time"],
                symbol=row["symbol"],
                config_hash=row["config_hash"],
                using_claude_default=row["using_claude_default"],
                current_state=row["current_state"],
                previous_state=row["previous_state"],
                state_history=state_history,
                state_reason=row["state_reason"] or "",
                close=float(row["close"]) if row["close"] is not None else None,
                sigma_p_24h=float(row["sigma_p_24h"]) if row["sigma_p_24h"] is not None else None,
                H=float(row["H"]) if row["H"] is not None else None,
                TF=float(row["TF"]) if row["TF"] is not None else None,
                OI=float(row["OI"]) if row["OI"] is not None else None,
                funding_rate=float(row["funding_rate"]) if row["funding_rate"] is not None else None,
                LV=float(row["LV"]) if row["LV"] is not None else None,
                absorption_ratio=float(row["absorption_ratio"]) if row["absorption_ratio"] is not None else None,
                OI_hurst=float(row["OI_hurst"]) if row["OI_hurst"] is not None else None,
                feature_completeness=float(row["feature_completeness"]),
                nav_usdt=float(row["nav_usdt"]),
                position_side=row["position_side"],
                position_size_usdt=float(row["position_size_usdt"]) if row["position_size_usdt"] is not None else 0.0,
                unrealized_pnl_usdt=float(row["unrealized_pnl"]) if row["unrealized_pnl"] is not None else 0.0,
                realized_pnl_usdt=float(row["realized_pnl"]) if row["realized_pnl"] is not None else 0.0,
                proposed_action=row["proposed_action"] or "",
                final_action=row["final_action"] or "",
                rule_id=row["rule_id"] or "",
                risk_triggered=row["risk_triggered"],
                risk_details=row["risk_details"] or "",
                fill_price=float(row["fill_price"]) if row["fill_price"] is not None else None,
                fill_size_usdt=float(row["fill_size_usdt"]) if row["fill_size_usdt"] is not None else None,
                fill_fee_usdt=float(row["fill_fee_usdt"]) if row["fill_fee_usdt"] is not None else None,
                realized_pnl_this_bar=float(row["realized_pnl_bar"]) if row["realized_pnl_bar"] is not None else None,
                risk_check_passed=row["risk_check_passed"],
            )
            result.append(trail)

        return result
