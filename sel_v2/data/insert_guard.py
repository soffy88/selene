"""Consecutive-INSERT-failure guard for the v2 collectors (optimization item #2).

The collectors previously wrapped every INSERT in `try/except` that only logged
"INSERT failed" and continued the loop. A schema mismatch therefore produced a
silently busy-looping container that wrote nothing for ~2 months. This guard
makes a sustained failure *fault loudly*: after N consecutive failures it raises
``InsertFailureLimitExceeded``, which the collector's ``main()`` lets propagate
so the process exits non-zero and Docker's restart + the healthcheck data-
freshness watchdog surface the outage.

A single success resets the counter, so transient/isolated errors don't trip it.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

DEFAULT_MAX_CONSECUTIVE = 10


class InsertFailureLimitExceeded(RuntimeError):
    """Raised when a collector hits too many consecutive INSERT failures."""


class InsertGuard:
    def __init__(self, name: str, max_consecutive: int = DEFAULT_MAX_CONSECUTIVE):
        self.name = name
        self.max_consecutive = max_consecutive
        self.consecutive = 0
        self.total_ok = 0
        self.total_failed = 0

    def ok(self) -> None:
        """Record a successful INSERT (resets the consecutive-failure counter)."""
        self.consecutive = 0
        self.total_ok += 1

    def fail(self, exc: BaseException) -> None:
        """Record a failed INSERT; raise once the consecutive limit is hit."""
        self.consecutive += 1
        self.total_failed += 1
        logger.error(
            "%s INSERT failed (%d consecutive, %d total): %s",
            self.name,
            self.consecutive,
            self.total_failed,
            exc,
        )
        if self.consecutive >= self.max_consecutive:
            raise InsertFailureLimitExceeded(
                f"{self.name}: {self.consecutive} consecutive INSERT failures — aborting to surface the fault"
            ) from exc
