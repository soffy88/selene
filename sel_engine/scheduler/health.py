"""
Health check for the bar-close scheduler.

Called by Docker healthcheck:
  python -c 'from sel_engine.scheduler.health import check; exit(0 if check() else 1)'

Returns True if the scheduler ran within the last 2 hours (healthy window is
intentionally wider than 1H to tolerate a single missed trigger on restart).
"""
import asyncio
import datetime
import os
from typing import Optional

import redis.asyncio as aioredis

_LAST_RUN_KEY = "sel:scheduler:last_run"
_HEALTHY_WINDOW_SECS = 7200  # 2 hours


def check() -> bool:
    """Synchronous wrapper — called by Docker CMD-SHELL healthcheck."""
    try:
        return asyncio.run(_async_check())
    except Exception:
        return False


async def _async_check() -> bool:
    url = os.environ.get("REDIS_URL", "redis://:changeme@redis:6379/0")
    r = aioredis.from_url(url, decode_responses=True)
    try:
        val: Optional[str] = await r.get(_LAST_RUN_KEY)
        if val is None:
            return False
        last_run = datetime.datetime.fromisoformat(val)
        now = datetime.datetime.now(datetime.timezone.utc)
        return (now - last_run).total_seconds() < _HEALTHY_WINDOW_SECS
    finally:
        await r.aclose()
