"""Single source of truth for data-source staleness judgment (GL1 T0.4).

Before this module, "is the data too old" was implicit and duplicated: the P1
derivatives adapter had its own 300s TTL check (md hash expiry), the state
machine's None-handling silently absorbed missing data, and nothing checked tick
or bar recency at all. This module is the one place staleness age → consequence
is decided, per the source/threshold/enforcement matrix in SEL2-SPEC-GL1 §3 T0.4:

| source     | threshold (default) | new entry      | position mgmt                | record          |
|------------|----------------------|-----------------|-------------------------------|-----------------|
| ticks      | > 90s                | S2 blocked      | CUSUM reversal exit paused;   | decision_trail  |
|            |                       |                 | time/hard stops continue      | reason code     |
| funding_oi | reuses 300s TTL       | S1 blocked      | unchanged                     | same            |
| bar_4h     | missing latest bar   | skip this cycle | unchanged                     | alert           |
| lob        | > 5min               | (none directly) | unchanged; entropy -> None,   | staleness event |
|            |                       |                 | tristate degrades naturally   |                 |

Thresholds are overridable (GL1 D2 "参数全部入 v2_strategy_params 可调") via the
`thresholds` dict passed into `is_stale` — callers source that from
`v2_strategy_params` keys `staleness_{source}_max_age_s`, falling back to
DEFAULT_THRESHOLDS_S here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

# Default thresholds in seconds. funding_oi reuses the P1 md-adapter's existing
# 300s TTL convention (see collector-consolidation memory) rather than inventing
# a second number for the same concept.
DEFAULT_THRESHOLDS_S: dict[str, float] = {
    "ticks": 90.0,
    "funding_oi": 300.0,
    "lob": 300.0,
}

# bar_4h has no fixed-age threshold — staleness there means "the most recently
# expected 4H boundary has passed (+ grace) and we still don't have that bar",
# which is a boundary check (see is_bar_stale), not a byte-for-byte age compare.
SOURCES = ("ticks", "funding_oi", "bar_4h", "lob")

REASON_CODES = {
    "ticks": "STALE_TICKS",
    "funding_oi": "STALE_FUNDING_OI",
    "bar_4h": "STALE_BAR",
    "lob": "STALE_LOB",
}


def is_stale(
    source: str,
    last_update: Optional[datetime],
    now: datetime,
    thresholds: Optional[dict] = None,
) -> bool:
    """True if `source`'s last known update is older than its threshold, or the
    source has never reported (last_update is None -> stale, conservative)."""
    if source not in DEFAULT_THRESHOLDS_S:
        raise ValueError(
            f"is_stale: unknown fixed-age source {source!r} (bar_4h uses is_bar_stale)"
        )
    if last_update is None:
        return True
    if last_update.tzinfo is None:
        last_update = last_update.replace(tzinfo=timezone.utc)
    threshold = (thresholds or DEFAULT_THRESHOLDS_S).get(
        source, DEFAULT_THRESHOLDS_S[source]
    )
    return (now - last_update).total_seconds() > threshold


def is_bar_stale(
    latest_bar_open: Optional[datetime],
    now: datetime,
    bar_interval_hours: float = 4.0,
    grace_minutes: float = 15.0,
) -> bool:
    """True if the most recently expected 4H bar boundary (+ grace period for it to
    close and arrive) has passed and `latest_bar_open` doesn't cover it yet."""
    if latest_bar_open is None:
        return True
    if latest_bar_open.tzinfo is None:
        latest_bar_open = latest_bar_open.replace(tzinfo=timezone.utc)
    step = int(bar_interval_hours)
    hour = (now.hour // step) * step
    expected_open = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if now < expected_open + timedelta(minutes=grace_minutes):
        # still inside the grace window for the current boundary's bar to arrive —
        # the previous boundary is the one that must already be covered.
        expected_open -= timedelta(hours=bar_interval_hours)
    return latest_bar_open < expected_open


@dataclass
class StalenessEnforcement:
    """What a (source, stale) reading means for strategy behaviour this cycle —
    the GL1 T0.4 matrix, as code. Every downstream consequence (entry gating,
    exit suppression, alerting) is derived from this, not re-decided ad hoc."""

    source: str
    stale: bool
    reason_code: Optional[str] = None
    block_s1_entry: bool = False
    block_s2_entry: bool = False
    pause_cusum_reversal_exit: bool = False  # drawdown / time stops still apply
    skip_bar: bool = False  # bar_4h only
    entropy_none: bool = False  # lob only — three-state machine degrades naturally


def enforcement_for(source: str, stale: bool) -> StalenessEnforcement:
    if source not in SOURCES:
        raise ValueError(f"enforcement_for: unknown source {source!r}")
    if not stale:
        return StalenessEnforcement(source=source, stale=False)
    reason = REASON_CODES[source]
    if source == "ticks":
        return StalenessEnforcement(
            source=source,
            stale=True,
            reason_code=reason,
            block_s2_entry=True,
            pause_cusum_reversal_exit=True,
        )
    if source == "funding_oi":
        return StalenessEnforcement(
            source=source,
            stale=True,
            reason_code=reason,
            block_s1_entry=True,
        )
    if source == "bar_4h":
        return StalenessEnforcement(
            source=source,
            stale=True,
            reason_code=reason,
            skip_bar=True,
        )
    # lob
    return StalenessEnforcement(
        source=source,
        stale=True,
        reason_code=reason,
        entropy_none=True,
    )
