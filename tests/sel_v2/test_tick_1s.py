"""The live 1s aggregator and the offline harness must price a second identically.

Wave S2G Part 1 acceptance. ~6% of seconds carry several ticks sharing the last
timestamp with different prices, so any disagreement between the two paths would
land on thousands of seconds a day and then masquerade as live-vs-offline
behaviour drift in the post-deploy reconciliation.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from sel_v2.data.tick_1s import (
    LAST_PRICE_PER_SECOND_SQL,
    Tick1sAggregator,
    fold_ticks_1s,
)

T0 = datetime(2026, 7, 13, 12, 0, 0, tzinfo=timezone.utc)


def _tick(offset_ms: int, price: float, trade_id: int):
    return (T0 + timedelta(milliseconds=offset_ms), price, str(trade_id))


# ── the rule itself ──────────────────────────────────────────────────────────


def test_second_price_is_the_highest_trade_id_not_the_last_arrival():
    """The tie the whole module exists for: same timestamp, different prices."""
    ticks = [
        _tick(750, 63750.0, 2780936524),
        _tick(750, 63749.9, 2780936525),
        _tick(750, 63749.6, 2780936526),  # highest id → this is the second's price
    ]
    assert fold_ticks_1s(ticks)[T0] == 63749.6


def test_a_later_timestamp_does_not_beat_a_higher_trade_id():
    """Arrival order is not the rule; trade_id is."""
    ticks = [_tick(900, 100.0, 5), _tick(100, 200.0, 9)]
    assert fold_ticks_1s(ticks)[T0] == 200.0


# ── order-insensitivity and idempotence ──────────────────────────────────────


def test_shuffled_input_gives_the_same_result():
    ticks = [_tick(i * 37 % 1000, 100.0 + i, 1000 + i) for i in range(50)]
    expected = fold_ticks_1s(ticks)
    for seed in range(5):
        shuffled = ticks[:]
        random.Random(seed).shuffle(shuffled)
        assert fold_ticks_1s(shuffled) == expected


def test_duplicates_collapse():
    ticks = [_tick(500, 100.0, 7), _tick(500, 100.0, 7), _tick(500, 100.0, 7)]
    assert fold_ticks_1s(ticks) == {T0: 100.0}


def test_ticks_split_across_seconds_stay_separate():
    ticks = [_tick(999, 100.0, 1), _tick(1000, 200.0, 2)]
    got = fold_ticks_1s(ticks)
    assert got[T0] == 100.0
    assert got[T0 + timedelta(seconds=1)] == 200.0


# ── live aggregator agrees with the offline fold ─────────────────────────────


def _feed(aggregator, ticks):
    for t in ticks:
        aggregator.add(*t)
    return dict(aggregator.snapshot())


def test_live_aggregator_matches_offline_fold_on_ordered_stream():
    ticks = [_tick(i * 20, 100.0 + (i % 7), 2000 + i) for i in range(120)]
    assert _feed(Tick1sAggregator(), ticks) == fold_ticks_1s(ticks)


def test_live_aggregator_matches_offline_fold_under_reorder_and_duplication():
    """A reconnect re-delivers a window out of order and with repeats — the live
    aggregator must still land where the offline harness lands."""
    base = [_tick(i * 13, 100.0 + (i % 11), 3000 + i) for i in range(200)]
    messy = base[:]
    messy += base[50:90]  # replayed window
    random.Random(99).shuffle(messy)
    assert _feed(Tick1sAggregator(), messy) == fold_ticks_1s(base)


def test_live_aggregator_matches_offline_fold_on_the_tie_case():
    ticks = [
        _tick(750, 63750.0, 2780936524),
        _tick(750, 63749.6, 2780936526),
        _tick(750, 63749.9, 2780936525),
    ]
    assert _feed(Tick1sAggregator(), ticks) == fold_ticks_1s(ticks)


# ── bounded memory, visible drops ────────────────────────────────────────────


def test_ticks_older_than_the_buffer_are_dropped_and_counted():
    agg = Tick1sAggregator(keep_seconds=10)
    agg.add(*_tick(0, 100.0, 1))
    agg.add(T0 + timedelta(seconds=60), 200.0, "2")
    agg.add(T0 - timedelta(seconds=120), 300.0, "3")  # far too late
    assert agg.dropped_late == 1
    assert T0 - timedelta(seconds=120) not in agg.snapshot()


def test_buffer_does_not_grow_without_bound():
    agg = Tick1sAggregator(keep_seconds=30)
    for i in range(500):
        agg.add(T0 + timedelta(seconds=i), 100.0 + i, str(4000 + i))
    assert len(agg.snapshot()) <= 31


# ── the SQL carries the same rule ────────────────────────────────────────────


def test_sql_orders_by_numeric_trade_id():
    """Guards the two ways this silently breaks: dropping the tie-break, or
    comparing the text column lexicographically."""
    assert "trade_id::bigint DESC" in LAST_PRICE_PER_SECOND_SQL
    assert "ORDER BY timestamp DESC, trade_id" in LAST_PRICE_PER_SECOND_SQL
