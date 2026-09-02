"""Consume-loop heartbeat used by livez/readyz (fail-closed when the loop dies)."""

from __future__ import annotations

import time
from typing import Any

_last: dict[str, float] = {}
_alive: dict[str, bool] = {}


def mark_consume(name: str = "default") -> None:
    _last[name] = time.time()
    _alive[name] = True


def consume_ready(name: str = "default", max_age_s: float = 45.0) -> bool:
    if not _alive.get(name):
        return False
    ts = _last.get(name, 0.0)
    return (time.time() - ts) <= max_age_s


def snapshot(name: str = "default") -> dict[str, Any]:
    ts = _last.get(name)
    return {
        "consume_alive": bool(_alive.get(name)),
        "consume_age_s": None if ts is None else round(time.time() - ts, 3),
        "consume_ready": consume_ready(name),
    }
