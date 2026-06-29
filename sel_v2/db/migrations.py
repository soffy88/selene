"""Apply the sel_v2 DB schema (v2_* tables) via asyncpg.

Mirrors sel_engine/db/migrations.py. The v2 collectors previously had no
process that applied their schema, so a fresh deploy left every v2_* table
either absent or shaped like the stale committed schema — which silently broke
every collector INSERT (optimization item #1/#2).

TimescaleDB-specific statements (create_hypertable / compression policies) are
downgraded to a warning when the extension is unavailable, so the table DDL
still applies against a plain PostgreSQL instance (e.g. local dev / CI).
"""
import logging
import os
from pathlib import Path

import asyncpg

logger = logging.getLogger(__name__)

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# Substrings that mark a statement as TimescaleDB-only (tolerated if it fails).
_TIMESCALE_MARKERS = (
    "create_hypertable",
    "add_compression_policy",
    "add_retention_policy",
    "timescaledb.compress",
)


def _is_tolerable(stmt: str) -> bool:
    """Statements whose failure must NOT abort startup:
    - TimescaleDB-only calls (extension may be absent on dev/CI), and
    - index creation (a UNIQUE index can't be built over a table that already
      contains duplicate rows; the collectors' INSERT ... ON CONFLICT DO NOTHING
      still works without the index — it just won't dedup until dupes are cleaned).
      Crashing the collector here would be far worse than skipping the index."""
    low = stmt.lower()
    if any(m in low for m in _TIMESCALE_MARKERS):
        return True
    if low.startswith("create unique index") or low.startswith("create index"):
        return True
    return False


def _split_statements(sql: str) -> list[str]:
    """Split schema SQL into executable statements.

    Strips '--' comments to end-of-line FIRST, so a semicolon inside a comment
    (e.g. "-- compress after 2 days; ...") can't create a comment-only fragment —
    executing a pure-comment string via asyncpg raised
    AttributeError('NoneType'...decode) and crashed the collector on startup.
    """
    no_comments = "\n".join(line.split("--", 1)[0] for line in sql.splitlines())
    return [s.strip() for s in no_comments.split(";") if s.strip()]


async def apply_schema(pool: asyncpg.Pool) -> None:
    """Execute schema.sql against the connected database (idempotent)."""
    sql = _SCHEMA_PATH.read_text()
    statements = _split_statements(sql)
    async with pool.acquire() as conn:
        for stmt in statements:
            try:
                await conn.execute(stmt)
            except asyncpg.exceptions.DuplicateTableError:
                pass  # idempotent
            except Exception as exc:  # noqa: BLE001
                if _is_tolerable(stmt):
                    logger.warning(
                        "Skipping non-critical statement (timescale absent / dup rows?): %s\n%s",
                        exc, stmt[:120],
                    )
                    continue
                logger.error("Migration statement failed: %s\n%s", exc, stmt[:200])
                raise
    logger.info("sel_v2 schema applied successfully")


async def run_migrations(pool: asyncpg.Pool | None = None) -> None:
    """Apply the schema using an existing pool, or one built from DB_URL."""
    own_pool = pool is None
    if own_pool:
        dsn = os.environ["DB_URL"].replace("postgresql+asyncpg://", "postgresql://")
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=3)
    try:
        await apply_schema(pool)
    finally:
        if own_pool:
            await pool.close()


if __name__ == "__main__":
    import asyncio
    asyncio.run(run_migrations())
