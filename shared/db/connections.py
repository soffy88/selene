"""
shared/db/connections.py

统一数据库连接工厂。
execution/main.py 引用了 shared.db.connections，此处补全。
同时提供 TimescaleDB async 连接池。
"""

import logging
import os
from typing import Optional

import asyncpg
import redis.asyncio as aioredis

logger = logging.getLogger("shared.db")

# ── Redis ─────────────────────────────────────────────
_redis_pool: Optional[aioredis.Redis] = None


async def get_redis() -> aioredis.Redis:
    global _redis_pool
    if _redis_pool is None:
        url = os.environ.get("REDIS_URL", "redis://:changeme@redis:6379/0")
        # Timeouts + periodic health-check so a transient DNS blip or a dropped
        # connection surfaces as a fast error instead of a silent hang (audit EC-13).
        _redis_pool = aioredis.from_url(
            url,
            decode_responses=False,
            socket_timeout=10,
            socket_connect_timeout=5,
            health_check_interval=60,
            retry_on_timeout=True,
        )
    return _redis_pool


async def redis_health() -> bool:
    try:
        r = await get_redis()
        return await r.ping()
    except Exception as e:
        logger.error(f"Redis health check failed: {e}")
        return False


# ── TimescaleDB ───────────────────────────────────────
_pg_pool: Optional[asyncpg.Pool] = None


async def get_pg() -> asyncpg.Pool:
    global _pg_pool
    if _pg_pool is None:
        dsn = os.environ.get(
            "TIMESCALE_URL",
            "postgresql://cw4:changeme@timescaledb:5432/cw4",
        ).replace("postgresql+asyncpg://", "postgresql://")
        # command_timeout / acquire timeout / inactive-connection recycling so a
        # long-idle pool doesn't hang forever on a stale connection (audit EC-13).
        _pg_pool = await asyncpg.create_pool(
            dsn,
            min_size=2,
            max_size=10,
            command_timeout=30,
            timeout=10,
            max_inactive_connection_lifetime=300,
        )
        logger.info("TimescaleDB pool created")
    return _pg_pool


async def wait_pg(timeout_s: float = 90.0) -> asyncpg.Pool:
    """Retry Postgres until it accepts connections. Qualification compose races
    pg_isready vs asyncpg; production boots should also survive a slow replica.
    """
    import asyncio
    import time

    deadline = time.time() + timeout_s
    last: Exception | None = None
    global _pg_pool
    while time.time() < deadline:
        try:
            _pg_pool = None
            return await get_pg()
        except Exception as exc:  # noqa: BLE001
            last = exc
            logger.warning("waiting for postgres: %s", exc)
            await asyncio.sleep(2)
    if last is not None:
        raise last
    raise RuntimeError("postgres unavailable")


async def pg_health() -> bool:
    try:
        pool = await get_pg()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return True
    except Exception as e:
        logger.error(f"PG health check failed: {e}")
        return False


async def close_all():
    global _redis_pool, _pg_pool
    if _redis_pool:
        await _redis_pool.close()
        _redis_pool = None
    if _pg_pool:
        await _pg_pool.close()
        _pg_pool = None
