"""Tests for sel_v2.offline.ict_lens (v2.2 ICT lens — ICT-2 structure + VPIN pilot stats)."""

import numpy as np

from sel_v2.offline.ict_lens import structure_series, vpin_pilot_stats
from sel_v2.offline.lens_common import compute_atr


def _trend_reversal_path():
    """Scripted uptrend (clean HH/HL swings) that then rolls over into a downtrend.
    Built with large swings vs a tight ATR so the 1.5x ATR zigzag confirms fast."""
    seg = []
    # uptrend: three pushes with pullbacks
    for base, top in [(100, 110), (104, 118), (112, 126)]:
        seg += list(np.linspace(base, top, 6))
        seg += list(np.linspace(top, top - 6, 4)[1:])
    # rollover: lower highs, lower lows
    for top, bottom in [(120, 104), (110, 94)]:
        seg += list(np.linspace(seg[-1], top, 3)[1:])
        seg += list(np.linspace(top, bottom, 6)[1:])
    close = np.array(seg, dtype=float)
    high = close + 0.3
    low = close - 0.3
    return high, low, close


def test_structure_series_trend_reversal_event_order():
    high, low, close = _trend_reversal_path()
    atr = compute_atr(high, low, close)
    states, events = structure_series(close, atr)
    assert len(states) == len(close)
    assert set(states) <= {"UP", "DOWN", "RANGE"}
    assert states[0] == "RANGE"  # nothing confirmed yet
    assert "UP" in states  # the uptrend is recognized once HH+HL confirm
    kinds = [e.kind for e in events]
    assert "BOS_UP" in kinds  # continuation breaks during the uptrend
    # the rollover produces a CHoCH before (or without) any BOS_DOWN
    assert "CHOCH_DOWN" in kinds
    first_choch_down = kinds.index("CHOCH_DOWN")
    assert "BOS_UP" in kinds[:first_choch_down]  # uptrend confirmed first


def test_structure_events_only_after_swings_confirm():
    high, low, close = _trend_reversal_path()
    atr = compute_atr(high, low, close)
    _states, events = structure_series(close, atr)
    # no event can fire before at least 4 swings (2 highs + 2 lows) confirmed;
    # the earliest possible confirm of the 4th swing is bar 4
    assert all(e.idx >= 4 for e in events)
    # event indices are causal and strictly ordered in time
    assert all(events[i].idx <= events[i + 1].idx for i in range(len(events) - 1))


def test_structure_series_flat_input_all_range_no_events():
    close = np.full(50, 100.0)
    atr = np.full(50, 1.0)
    states, events = structure_series(close, atr)
    assert states == ["RANGE"] * 50
    assert events == []


# ── vpin_pilot_stats ─────────────────────────────────────────────────────────


def test_vpin_pilot_stats_empty_and_filled():
    assert vpin_pilot_stats([], [])["n_vpin_points"] == 0

    from datetime import datetime, timedelta, timezone

    t0 = datetime(2026, 7, 6, tzinfo=timezone.utc)
    series = [(t0 + timedelta(minutes=30 * i), 0.2 + 0.01 * (i % 10), 0.25 + 0.01 * (i % 10)) for i in range(120)]
    stats = vpin_pilot_stats(series, bucket_minutes=[30.0] * 120)
    assert stats["n_vpin_points"] == 120
    d = stats["distribution"]
    assert d["p50"] <= d["p90"] <= d["p95"] <= d["p97"] <= stats["max"]
    assert -1.0 <= stats["lag1_autocorr"] <= 1.0
    assert stats["side_vs_bvc_corr"] > 0.99  # constructed as shifted copies
    assert stats["bucket_minutes"]["median"] == 30.0
