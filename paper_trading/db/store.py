"""AsyncPG persistence layer for paper trading data."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from paper_trading.schema import AccountState, Decision, Position, SimulatedFill

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


class PaperTradingStore:
    """Thin wrapper around an asyncpg connection pool."""

    # ------------------------------------------------------------------
    @staticmethod
    async def run_migrations(pool) -> None:
        """Execute schema.sql against the database (idempotent)."""
        sql = _SCHEMA_PATH.read_text()
        async with pool.acquire() as conn:
            await conn.execute(sql)

    # ------------------------------------------------------------------
    @staticmethod
    async def upsert_decision(pool, decision: Decision) -> None:
        """Insert a Decision record (no ON CONFLICT — every bar is unique)."""
        sql = """
            INSERT INTO sel_decisions
                (time, symbol, action, rule_id, config_hash,
                 current_state, previous_state,
                 position_multiplier, target_size_usdt, reason)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
        """
        async with pool.acquire() as conn:
            await conn.execute(
                sql,
                decision.time,
                decision.symbol,
                decision.action.value,
                decision.rule_id,
                decision.config_hash,
                decision.current_state,
                decision.previous_state,
                decision.position_multiplier,
                decision.target_size_usdt,
                decision.reason,
            )

    # ------------------------------------------------------------------
    @staticmethod
    async def insert_fill(pool, fill: SimulatedFill) -> None:
        """Insert a SimulatedFill record."""
        sql = """
            INSERT INTO sel_paper_trades
                (time, symbol, side, action,
                 requested_size_usdt, fill_price, fill_size_usdt,
                 slippage_usdt, fee_usdt, net_size_usdt, config_hash)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
        """
        async with pool.acquire() as conn:
            await conn.execute(
                sql,
                fill.time,
                fill.symbol,
                fill.side.value,
                fill.action,
                fill.requested_size_usdt,
                fill.fill_price,
                fill.fill_size_usdt,
                fill.slippage_usdt,
                fill.fee_usdt,
                fill.net_size_usdt,
                fill.config_hash,
            )

    # ------------------------------------------------------------------
    @staticmethod
    async def upsert_position_snapshot(
        pool,
        time,
        symbol: str,
        position: Optional[Position],
    ) -> None:
        """Insert a position snapshot (NULL columns when no position open)."""
        sql = """
            INSERT INTO sel_positions
                (time, symbol, side, open_time, open_price,
                 size_usdt, current_size_usdt, unrealized_pnl_usdt, entry_fee_usdt)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
        """
        async with pool.acquire() as conn:
            await conn.execute(
                sql,
                time,
                symbol,
                position.side.value if position else None,
                position.open_time if position else None,
                position.open_price if position else None,
                position.size_usdt if position else None,
                position.current_size_usdt if position else None,
                position.unrealized_pnl_usdt if position else None,
                position.entry_fee_usdt if position else None,
            )

    # ------------------------------------------------------------------
    @staticmethod
    async def upsert_account_state(pool, state: AccountState) -> None:
        """Insert an AccountState snapshot."""
        sql = """
            INSERT INTO sel_account_state
                (time, nav_usdt, realized_pnl_usdt, unrealized_pnl_usdt,
                 position_side, position_size_usdt, daily_drawdown_pct,
                 consecutive_losses, is_paused, pause_until, cascade_cooling)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
        """
        async with pool.acquire() as conn:
            await conn.execute(
                sql,
                state.time,
                state.nav_usdt,
                state.realized_pnl_usdt,
                state.unrealized_pnl_usdt,
                state.position_side,
                state.position_size_usdt,
                state.daily_drawdown_pct,
                state.consecutive_losses,
                state.is_paused,
                state.pause_until,
                state.cascade_cooling,
            )
