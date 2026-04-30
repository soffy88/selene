"""
Dual asyncpg connection pools for sel v2.

SELENE_POOL: selene database (main read/write)
HELIXA_POOL: helixa database (read-only, Wave 5+)

Usage:
    await init_pools()
    async with (await get_helixa_pool()).acquire() as conn:
        rows = await conn.fetch(...)
    await close_pools()
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import asyncpg

logger = logging.getLogger(__name__)

_selene_pool: Optional[asyncpg.Pool] = None
_helixa_pool: Optional[asyncpg.Pool] = None


def _selene_url() -> str:
    if url := os.getenv("SELENE_DB_URL"):
        return url
    host = os.getenv("POSTGRES_HOST", "platform-postgres")
    port = os.getenv("POSTGRES_PORT", "5432")
    user = os.getenv("POSTGRES_USER", "selene_app")
    pw = os.getenv("POSTGRES_PASSWORD", "2eb0eac6f7a01f5c41de2dacffa928474fb575c884365ac5")
    return f"postgresql://{user}:{pw}@{host}:{port}/selene"


def _helixa_url() -> str:
    if url := os.getenv("HELIXA_DB_URL"):
        return url
    host = os.getenv("POSTGRES_HOST", "platform-postgres")
    port = os.getenv("POSTGRES_PORT", "5432")
    user = os.getenv("POSTGRES_USER", "selene_app")
    pw = os.getenv("POSTGRES_PASSWORD", "2eb0eac6f7a01f5c41de2dacffa928474fb575c884365ac5")
    return f"postgresql://{user}:{pw}@{host}:{port}/helixa"


async def init_pools(
    selene_min: int = 1,
    selene_max: int = 5,
    helixa_min: int = 1,
    helixa_max: int = 3,
) -> None:
    """Initialize both connection pools. Call once at startup."""
    global _selene_pool, _helixa_pool

    logger.info("Initializing SELENE_POOL (%s)", _selene_url())
    _selene_pool = await asyncpg.create_pool(
        _selene_url(), min_size=selene_min, max_size=selene_max
    )
    logger.info("Initializing HELIXA_POOL (%s)", _helixa_url())
    _helixa_pool = await asyncpg.create_pool(
        _helixa_url(), min_size=helixa_min, max_size=helixa_max
    )
    logger.info("Both pools ready")


async def close_pools() -> None:
    """Close both pools. Call at shutdown."""
    global _selene_pool, _helixa_pool
    if _selene_pool:
        await _selene_pool.close()
        _selene_pool = None
    if _helixa_pool:
        await _helixa_pool.close()
        _helixa_pool = None
    logger.info("Both pools closed")


async def get_selene_pool() -> asyncpg.Pool:
    """Return the SELENE_POOL, initializing lazily if needed."""
    global _selene_pool
    if _selene_pool is None:
        _selene_pool = await asyncpg.create_pool(_selene_url(), min_size=1, max_size=5)
    return _selene_pool


async def get_helixa_pool() -> asyncpg.Pool:
    """Return the HELIXA_POOL, initializing lazily if needed."""
    global _helixa_pool
    if _helixa_pool is None:
        _helixa_pool = await asyncpg.create_pool(_helixa_url(), min_size=1, max_size=3)
    return _helixa_pool
