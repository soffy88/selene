"""Canonical 1-second tick aggregation (Wave S2G Part 1).

ONE definition of "the price of second N", shared by the live S2 channel and the
offline replay harness. They must not merely agree by convention: about **6% of
seconds carry several ticks sharing the last timestamp with different prices**
(measured on 2026-07-13: 6,433 of 77,303 seconds tied, 4,692 of those with
differing prices). Two independent implementations would diverge on exactly those
seconds, and the divergence would then contaminate the live-vs-offline
reconciliation that is supposed to detect behaviour drift.

The rule: **the second's price is the price of its highest trade_id.** trade_id is
monotonic per venue (verified over a 557,274-tick sample: zero inversions), so the
maximum is genuinely the last trade of that second — semantics, not just a
deterministic tie-break. Ordering by timestamp alone leaves the pick to Postgres,
which is undefined and was observed to vary between runs 8 seconds apart.

trade_id is stored as text, so it is compared numerically here and cast to bigint
in SQL; a plain lexicographic DESC would silently invert at a digit-count rollover
(every id is 10 digits today, so this changes nothing yet).

Reconnects and replay windows re-deliver ticks out of order and duplicated. Both
entry points below are order-insensitive and idempotent: feeding the same tick
twice, or late, yields the same second→price map.
"""

from __future__ import annotations

from typing import Iterable, Mapping

# The canonical SQL. Callers select their own time window and symbol.
LAST_PRICE_PER_SECOND_SQL = """
SELECT date_trunc('second', timestamp) AS s,
       (array_agg(price ORDER BY timestamp DESC, trade_id::bigint DESC))[1] AS px
FROM v2_ticks
WHERE symbol = $1 AND ($2::timestamptz IS NULL OR timestamp >= $2::timestamptz)
                  AND ($3::timestamptz IS NULL OR timestamp <  $3::timestamptz)
GROUP BY 1 ORDER BY 1
"""


def _key(trade_id) -> int:
    """trade_id as a number. Raises on a non-numeric id rather than falling back to
    string order — a silently mis-ordered id would corrupt exactly the ~6% of
    seconds this module exists to disambiguate."""
    return int(trade_id)


def fold_ticks_1s(ticks: Iterable[tuple]) -> dict:
    """(second → price) from raw ticks, order-insensitive and idempotent.

    `ticks` yields (timestamp, price, trade_id). The winning tick for a second is
    the one with the highest trade_id; timestamp only decides which second the
    tick belongs to. Duplicates collapse because the comparison is on trade_id,
    not on arrival.
    """
    best: dict = {}  # second → (trade_id, price)
    for ts, price, trade_id in ticks:
        second = ts.replace(microsecond=0)
        tid = _key(trade_id)
        current = best.get(second)
        if current is None or tid > current[0]:
            best[second] = (tid, float(price))
    return {s: p for s, (_, p) in best.items()}


class Tick1sAggregator:
    """Streaming form of `fold_ticks_1s` for the live engine.

    Holds only the seconds still open plus a bounded tail, so a long-running
    engine does not accumulate the whole session. Late ticks for a second still
    in the buffer correct it; ticks older than the buffer are dropped and counted,
    because silently rewriting a second the strategy has already consumed would be
    worse than a visible gap.
    """

    def __init__(self, keep_seconds: int = 3600) -> None:
        self.keep_seconds = keep_seconds
        self._best: dict = {}  # second → (trade_id, price)
        self.dropped_late = 0

    def add(self, ts, price, trade_id) -> None:
        second = ts.replace(microsecond=0)
        if self._best:
            newest = max(self._best)
            if (newest - second).total_seconds() > self.keep_seconds:
                self.dropped_late += 1
                return
        tid = _key(trade_id)
        current = self._best.get(second)
        if current is None or tid > current[0]:
            self._best[second] = (tid, float(price))
        self._evict()

    def _evict(self) -> None:
        if not self._best:
            return
        newest = max(self._best)
        cutoff = newest.timestamp() - self.keep_seconds
        for s in [s for s in self._best if s.timestamp() < cutoff]:
            del self._best[s]

    def snapshot(self) -> Mapping:
        """second → price for everything currently buffered."""
        return {s: p for s, (_, p) in self._best.items()}
