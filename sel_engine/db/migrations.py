"""Apply sel_engine DB schema migrations via asyncpg."""

import logging
import os
from pathlib import Path

import asyncpg

logger = logging.getLogger(__name__)

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")


async def apply_schema(pool: asyncpg.Pool) -> None:
    """Execute schema.sql against the connected TimescaleDB instance."""
    sql = _SCHEMA_PATH.read_text()
    # Split on semicolons so asyncpg executes one statement at a time.
    statements = [s.strip() for s in sql.split(";") if s.strip()]
    async with pool.acquire() as conn:
        for stmt in statements:
            try:
                await conn.execute(stmt)
            except asyncpg.exceptions.DuplicateTableError:
                pass  # idempotent
            except Exception as exc:
                logger.error("Migration statement failed: %s\n%s", exc, stmt[:200])
                raise
    logger.info("sel_engine schema applied successfully")


async def run_migrations() -> None:
    dsn = os.environ.get(
        "TIMESCALE_URL",
        "postgresql://cw4:changeme@timescaledb:5432/cw4",
    ).replace("postgresql+asyncpg://", "postgresql://")
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=3)
    try:
        await apply_schema(pool)
    finally:
        await pool.close()


if __name__ == "__main__":
    import asyncio

    asyncio.run(run_migrations())
