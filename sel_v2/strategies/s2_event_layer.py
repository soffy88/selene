"""S2 event-ification and throttle layer (Wave S2G, Parts 2-3).

Sits between the 1s CUSUM-Short accumulator and the existing Step 2-5 chain. The
chain itself is untouched — this layer only decides WHICH triggers become an
evaluable event, and which events are allowed through.

Why it exists: at its designed 1s granularity the accumulator produces ~548
distinct excursions/day (S2G-0), and feeding those straight into Step 2-5 is a
firehose. Two frozen rules shape it:

  cluster        excursions within 300s of each other are one cluster
  confirmation   a cluster is evaluated when its SECOND distinct excursion
                 arrives — never on the first

The confirmation rule is not arbitrary. S2G-0 stratified classification rate by
cluster length: singletons classify 48.2% of the time, clusters of 11+ reach
62.1%, rising monotonically. Waiting for a second excursion discards the weakest
population before it ever reaches Step 3.

Throttle, applied at the exit:
  THROTTLED_POSITION   an S2 position in the same direction is already open
  THROTTLED_DAILY      4 entries already taken this UTC day (Wiki-ruled cap)
Both are RECORDED, not silently dropped, so "what did we pass up" stays auditable.

Frozen parameters (do not tune): 300s cooldown, confirm on the 2nd excursion,
4 entries per UTC day.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

# ── frozen (Wave S2G) ────────────────────────────────────────────────────────
CLUSTER_COOLDOWN_S = 300
CONFIRM_ON_EXCURSION = 2  # evaluate the cluster on its 2nd distinct excursion
DAILY_ENTRY_CAP = 4
EXCURSION_GAP_S = 1  # > 1s apart = a distinct excursion

THROTTLED_POSITION = "THROTTLED_POSITION"
THROTTLED_DAILY = "THROTTLED_DAILY"


@dataclass
class S2Event:
    """One evaluable S2 opportunity — a confirmed cluster, not a raw trigger."""

    event_id: str
    cluster_start_ts: datetime
    eval_ts: datetime
    direction: Literal["LONG", "SHORT"]
    peak: float
    excursion_count: int

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "cluster_start_ts": self.cluster_start_ts.isoformat(),
            "eval_ts": self.eval_ts.isoformat(),
            "direction": self.direction,
            "peak": self.peak,
            "excursion_count": self.excursion_count,
        }


@dataclass
class _Cluster:
    start_ts: datetime
    last_ts: datetime
    direction: str
    excursions: int = 1
    peak: float = 0.0
    emitted: bool = False


@dataclass
class S2EventLayer:
    """Trigger stream → confirmed events. Pure: no DB, no clock of its own.

    All timestamps come from the caller, so a replay and the live engine walk the
    same path.
    """

    cooldown_s: int = CLUSTER_COOLDOWN_S
    confirm_on: int = CONFIRM_ON_EXCURSION
    daily_cap: int = DAILY_ENTRY_CAP
    _cluster: Optional[_Cluster] = field(default=None, repr=False)
    _last_trigger_ts: Optional[datetime] = field(default=None, repr=False)
    # entries actually taken, per UTC day, for the daily cap
    _entries_by_day: dict = field(default_factory=dict, repr=False)

    # ── event-ification ──────────────────────────────────────────────────────

    def on_trigger(
        self, ts: datetime, direction: str, peak: float
    ) -> Optional[S2Event]:
        """Feed one raw CUSUM trigger. Returns an S2Event only at confirmation.

        Consecutive seconds of the same excursion collapse: only a gap greater
        than EXCURSION_GAP_S starts a new distinct excursion, which is what
        "excursion" means in S2G-0's 548/day figure.
        """
        prev = self._last_trigger_ts
        self._last_trigger_ts = ts
        is_distinct = prev is None or (ts - prev).total_seconds() > EXCURSION_GAP_S
        if not is_distinct:
            if self._cluster is not None:
                self._cluster.peak = max(self._cluster.peak, peak)
            return None

        c = self._cluster
        # A gap wider than the cooldown, or no cluster yet, opens a new one.
        if c is None or (ts - c.last_ts).total_seconds() > self.cooldown_s:
            self._cluster = _Cluster(
                start_ts=ts, last_ts=ts, direction=direction, peak=peak
            )
            return None

        c.last_ts = ts
        c.excursions += 1
        c.peak = max(c.peak, peak)
        # Direction is taken from the excursion that confirms the cluster: it is
        # the one being evaluated, and the first excursion may have been the
        # opposite side of the same disturbance.
        c.direction = direction

        if c.emitted or c.excursions < self.confirm_on:
            return None  # already evaluated this cluster, or not confirmed yet

        c.emitted = True
        return S2Event(
            event_id=str(uuid.uuid4()),
            cluster_start_ts=c.start_ts,
            eval_ts=ts,
            direction=direction,
            peak=c.peak,
            excursion_count=c.excursions,
        )

    # ── throttle ─────────────────────────────────────────────────────────────

    def throttle_reason(
        self,
        event: S2Event,
        open_s2_directions: set,
        now: Optional[datetime] = None,
    ) -> Optional[str]:
        """None = may proceed to Step 2. Otherwise the reason, for the record."""
        if event.direction in open_s2_directions:
            return THROTTLED_POSITION
        day = (now or event.eval_ts).astimezone(timezone.utc).date()
        if self._entries_by_day.get(day, 0) >= self.daily_cap:
            return THROTTLED_DAILY
        return None

    def record_entry(self, ts: datetime) -> None:
        """Call when an event actually becomes an entry — this is what the daily
        cap counts. Throttled or Step-2..5-rejected events do NOT count."""
        day = ts.astimezone(timezone.utc).date()
        self._entries_by_day[day] = self._entries_by_day.get(day, 0) + 1
        # keep the map from growing forever
        cutoff = ts.astimezone(timezone.utc).date() - timedelta(days=7)
        for d in [d for d in self._entries_by_day if d < cutoff]:
            del self._entries_by_day[d]

    def entries_today(self, now: datetime) -> int:
        return self._entries_by_day.get(now.astimezone(timezone.utc).date(), 0)
