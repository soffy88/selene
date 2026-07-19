"""S2 event-ification + throttle, and the whole chain end to end (Wave S2G 2-3).

The layer-by-layer tests pin each frozen rule. The end-to-end tests at the bottom
drive a synthetic tick stream through every stage the live engine will use —
1s aggregation → CUSUM → cluster confirmation → throttle → Step 2 admission —
because the layers can each be correct and still be wired together wrongly.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from sel_v2.data.tick_1s import DENSITY_MIN_TICKS, Tick1sAggregator, fold_ticks_1s
from sel_v2.strategies.cusum_short import CUSUMShort
from sel_v2.strategies.s2_event_layer import (
    CLUSTER_COOLDOWN_S,
    CONFIRM_ON_EXCURSION,
    DAILY_ENTRY_CAP,
    THROTTLED_DAILY,
    THROTTLED_POSITION,
    S2EventLayer,
)

T0 = datetime(2026, 7, 13, 12, 0, 0, tzinfo=timezone.utc)


def _at(sec: float) -> datetime:
    return T0 + timedelta(seconds=sec)


# ── frozen parameters ────────────────────────────────────────────────────────


def test_frozen_parameters():
    assert CLUSTER_COOLDOWN_S == 300
    assert CONFIRM_ON_EXCURSION == 2
    assert DAILY_ENTRY_CAP == 4


# ── event-ification ──────────────────────────────────────────────────────────


def test_first_excursion_never_emits():
    """Singletons classify worst (48.2% vs 62.1%), so they are never evaluated."""
    layer = S2EventLayer()
    assert layer.on_trigger(_at(0), "LONG", 5.0) is None


def test_second_excursion_confirms_the_cluster():
    layer = S2EventLayer()
    layer.on_trigger(_at(0), "LONG", 5.0)
    ev = layer.on_trigger(_at(30), "LONG", 6.5)
    assert ev is not None
    assert ev.excursion_count == 2
    assert ev.cluster_start_ts == _at(0)
    assert ev.eval_ts == _at(30)
    assert ev.peak == 6.5  # cluster max, not just the confirming excursion


def test_consecutive_seconds_are_one_excursion_not_two():
    """A trigger that stays true second after second is ONE excursion; treating
    each second as an excursion would confirm every cluster instantly."""
    layer = S2EventLayer()
    assert layer.on_trigger(_at(0), "LONG", 5.0) is None
    assert layer.on_trigger(_at(1), "LONG", 5.5) is None  # same excursion
    assert layer.on_trigger(_at(2), "LONG", 6.0) is None  # still the same


def test_cluster_emits_only_once():
    layer = S2EventLayer()
    layer.on_trigger(_at(0), "LONG", 5.0)
    assert layer.on_trigger(_at(30), "LONG", 6.0) is not None
    for s in (60, 120, 200, 299):
        assert layer.on_trigger(_at(s), "LONG", 7.0) is None


def test_gap_beyond_cooldown_starts_a_new_cluster():
    layer = S2EventLayer()
    layer.on_trigger(_at(0), "LONG", 5.0)
    layer.on_trigger(_at(30), "LONG", 6.0)  # emits
    # > 300s after the cluster's last excursion → new cluster, needs 2 again
    assert layer.on_trigger(_at(30 + CLUSTER_COOLDOWN_S + 1), "LONG", 8.0) is None
    assert layer.on_trigger(_at(30 + CLUSTER_COOLDOWN_S + 40), "LONG", 9.0) is not None


def test_exactly_at_the_cooldown_boundary_stays_in_the_cluster():
    layer = S2EventLayer()
    layer.on_trigger(_at(0), "LONG", 5.0)
    layer.on_trigger(_at(10), "LONG", 6.0)  # emits, cluster last_ts = 10
    # 300s later exactly — not > cooldown, so still the same (already emitted)
    assert layer.on_trigger(_at(10 + CLUSTER_COOLDOWN_S), "LONG", 9.0) is None


def test_confirming_excursion_sets_the_direction():
    layer = S2EventLayer()
    layer.on_trigger(_at(0), "LONG", 5.0)
    ev = layer.on_trigger(_at(20), "SHORT", 6.0)
    assert ev.direction == "SHORT"


# ── throttle ─────────────────────────────────────────────────────────────────


def _event(layer, base=0, direction="LONG"):
    layer.on_trigger(_at(base), direction, 5.0)
    return layer.on_trigger(_at(base + 10), direction, 6.0)


def test_same_direction_position_throttles():
    layer = S2EventLayer()
    ev = _event(layer)
    assert layer.throttle_reason(ev, {"LONG"}) == THROTTLED_POSITION
    assert layer.throttle_reason(ev, {"SHORT"}) is None  # other side is fine
    assert layer.throttle_reason(ev, set()) is None


def test_daily_cap_throttles_after_four_entries():
    layer = S2EventLayer()
    ev = _event(layer)
    for i in range(DAILY_ENTRY_CAP):
        assert layer.throttle_reason(ev, set()) is None
        layer.record_entry(_at(i))
    assert layer.throttle_reason(ev, set()) == THROTTLED_DAILY


def test_daily_cap_counts_entries_not_events():
    """Throttled or Step-rejected events must not consume the day's budget."""
    layer = S2EventLayer()
    ev = _event(layer)
    for _ in range(10):
        layer.throttle_reason(ev, {"LONG"})  # throttled, never recorded
    assert layer.throttle_reason(ev, set()) is None
    assert layer.entries_today(_at(0)) == 0


def test_daily_cap_resets_on_the_utc_day_boundary():
    layer = S2EventLayer()
    ev = _event(layer)
    for i in range(DAILY_ENTRY_CAP):
        layer.record_entry(_at(i))
    assert layer.throttle_reason(ev, set()) == THROTTLED_DAILY
    next_day = _at(0) + timedelta(days=1)
    assert layer.throttle_reason(ev, set(), now=next_day) is None


def test_position_throttle_takes_precedence_over_daily():
    layer = S2EventLayer()
    ev = _event(layer)
    for i in range(DAILY_ENTRY_CAP):
        layer.record_entry(_at(i))
    assert layer.throttle_reason(ev, {"LONG"}) == THROTTLED_POSITION


# ── density guard ────────────────────────────────────────────────────────────


def test_sparse_feed_is_flagged_low_density():
    agg = Tick1sAggregator()
    for i in range(DENSITY_MIN_TICKS - 1):
        agg.add(_at(i), 100.0, str(1000 + i))
    assert agg.is_low_density(_at(DENSITY_MIN_TICKS - 2)) is True


def test_dense_feed_is_not_flagged():
    agg = Tick1sAggregator()
    for i in range(DENSITY_MIN_TICKS + 5):
        agg.add(_at(0), 100.0 + i, str(2000 + i))
    assert agg.is_low_density(_at(0)) is False


# ── end to end: tick stream → aggregate → CUSUM → cluster → throttle → Step 2 ──


def _drive(ticks, layer, acc, agg, open_dirs=(), record_entries=True):
    """The exact chain the live engine will run. Returns (admitted, throttled,
    suppressed_low_density)."""
    for ts, px, tid in ticks:
        agg.add(ts, px, tid)

    admitted, throttled, suppressed = [], [], 0
    prices = agg.snapshot()
    seconds = sorted(prices)
    prev_px = None
    for s in seconds:
        px = prices[s]
        if prev_px is None:
            prev_px = px
            continue
        # crude z proxy: the engine standardises, the chain shape is what matters
        z = (px - prev_px) / max(abs(prev_px) * 1e-4, 1e-9)
        prev_px = px
        trig = acc.update(z, s.timestamp())
        if not trig.triggered:
            continue
        if agg.is_low_density(s):
            suppressed += 1  # z still computed, trigger suppressed and counted
            continue
        ev = layer.on_trigger(
            s, trig.direction, max(trig.cusum_positive, trig.cusum_negative)
        )
        if ev is None:
            continue  # unconfirmed cluster — never reaches Step 2
        reason = layer.throttle_reason(ev, set(open_dirs))
        if reason:
            throttled.append((ev, reason))
        else:
            admitted.append(ev)
            if record_entries:
                layer.record_entry(ev.eval_ts)
    return admitted, throttled, suppressed


def _dense_shock_stream(n_shocks: int, spacing_s: int, ticks_per_sec: int = 20):
    """Dense feed with periodic price shocks sized to produce SEPARATE excursions.

    The shock must lift C+ just past the cold-start h=2.0 and then let drift k=0.5
    decay it back within a few seconds. A large shock instead pins C+ far above h
    for hundreds of seconds, which is correctly read as ONE long excursion and
    therefore never confirms a cluster — the failure mode this fixture is built to
    avoid, not to hide. With the z proxy below (z = dpx / (px*1e-4)), dpx=0.03
    gives z~3.
    """
    ticks, tid = [], 1_000_000
    for sec in range(n_shocks * spacing_s + 10):
        shock = (sec % spacing_s == 0) and sec > 0
        for k in range(ticks_per_sec):
            px = 100.0 + (0.03 if shock else 0.0) + k * 1e-9
            ticks.append((_at(sec) + timedelta(milliseconds=k), px, str(tid)))
            tid += 1
    return ticks


def _burst_stream(n_bursts: int, ticks_per_sec: int = 20):
    """One CONFIRMED cluster per burst: two shocks 20s apart (inside the 300s
    cooldown, so the second confirms), bursts separated by more than the cooldown
    so they never merge. This is what a test of the throttle needs — a stream that
    actually reaches Step 2, rather than one that fails to emit and passes
    vacuously.
    """
    ticks, tid = [], 2_000_000
    period = CLUSTER_COOLDOWN_S + 120
    for b in range(n_bursts):
        base = b * period
        shock_secs = {base + 5, base + 25}
        for sec in range(base, base + period):
            for k in range(ticks_per_sec):
                px = 100.0 + (0.03 if sec in shock_secs else 0.0) + k * 1e-9
                ticks.append((_at(sec) + timedelta(milliseconds=k), px, str(tid)))
                tid += 1
    return ticks


def test_end_to_end_chain_admits_confirmed_clusters_only():
    layer, acc, agg = (
        S2EventLayer(),
        CUSUMShort(),
        Tick1sAggregator(keep_seconds=10_000),
    )
    admitted, throttled, suppressed = _drive(
        _dense_shock_stream(6, 20), layer, acc, agg
    )
    # something got through the whole chain
    assert admitted, "no event survived the chain"
    # and every admitted event is a CONFIRMED cluster, never a singleton
    assert all(e.excursion_count >= CONFIRM_ON_EXCURSION for e in admitted)
    assert suppressed == 0  # the feed is dense


def test_end_to_end_daily_cap_bounds_admissions():
    layer, acc, agg = (
        S2EventLayer(),
        CUSUMShort(),
        Tick1sAggregator(keep_seconds=10_000),
    )
    admitted, throttled, _ = _drive(_burst_stream(9), layer, acc, agg)
    # not vacuous: the stream must actually produce more events than the cap
    assert len(admitted) + len(throttled) > DAILY_ENTRY_CAP, "stream never reached the cap"
    assert len(admitted) == DAILY_ENTRY_CAP, "daily cap did not bound entries"
    assert any(r == THROTTLED_DAILY for _, r in throttled)


def test_end_to_end_open_position_throttles_same_direction():
    layer, acc, agg = (
        S2EventLayer(),
        CUSUMShort(),
        Tick1sAggregator(keep_seconds=10_000),
    )
    admitted, throttled, _ = _drive(
        _burst_stream(3), layer, acc, agg, open_dirs=("LONG", "SHORT")
    )
    assert admitted == [], "a position was already open in both directions"
    assert throttled and all(r == THROTTLED_POSITION for _, r in throttled)


def test_end_to_end_sparse_feed_suppresses_instead_of_firing():
    """A feed thin enough to manufacture z-scores must not produce entries."""
    layer, acc, agg = (
        S2EventLayer(),
        CUSUMShort(),
        Tick1sAggregator(keep_seconds=10_000),
    )
    ticks, tid = [], 5_000_000
    for sec in range(0, 600, 30):  # one tick every 30s — far below the guard
        ticks.append((_at(sec), 100.0 + (5.0 if sec % 120 == 0 else 0.0), str(tid)))
        tid += 1
    admitted, _, suppressed = _drive(ticks, layer, acc, agg)
    assert admitted == []
    assert suppressed > 0, "sparse feed produced no suppressions to count"


def test_end_to_end_aggregation_matches_the_offline_fold():
    """The chain must start from the same 1s prices the offline harness derives."""
    ticks = _dense_shock_stream(3, 20)
    agg = Tick1sAggregator(keep_seconds=10_000)
    for t in ticks:
        agg.add(*t)
    assert dict(agg.snapshot()) == fold_ticks_1s(ticks)


@pytest.mark.parametrize("shuffle_seed", [1, 2, 3])
def test_end_to_end_is_insensitive_to_tick_arrival_order(shuffle_seed):
    """Reconnects re-deliver out of order; the admitted set must not depend on it."""
    import random

    ticks = _dense_shock_stream(6, 40)
    ordered = _drive(
        ticks, S2EventLayer(), CUSUMShort(), Tick1sAggregator(keep_seconds=10_000)
    )[0]
    messy = ticks[:]
    random.Random(shuffle_seed).shuffle(messy)
    shuffled = _drive(
        messy, S2EventLayer(), CUSUMShort(), Tick1sAggregator(keep_seconds=10_000)
    )[0]
    assert [e.eval_ts for e in ordered] == [e.eval_ts for e in shuffled]
