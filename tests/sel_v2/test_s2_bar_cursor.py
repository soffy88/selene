"""process_frame's 1s cursor must slice the stream without losing or repeating a second.

The Part 5 reconciliation proved `_advance_1s_channel` itself is transparent — bar
chunks give the same events as one continuous pass — but it drove the chunking
with its OWN loop. The cursor that actually feeds it (`prev_bar_unix`, advanced
once per bar inside process_frame) was not covered.

That gap matters because of how it would fail: a mis-advanced cursor drops or
double-counts seconds at bar boundaries, and dropped events look exactly like "a
quiet market". No exception, no log — just fewer entries than there should be.
"""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np

from sel_v2.paper.strategy_engine import PaperStrategyEngine
from sel_v2.strategies.cusum_short import CUSUMShort
from sel_v2.strategies.s2_event_layer import S2EventLayer

T0 = datetime(2026, 7, 13, tzinfo=timezone.utc)
BAR_S = 4 * 3600


def _engine(layer):
    return PaperStrategyEngine(
        total_nav_usdt=100_000,
        skip_hawkes=True,
        skip_tda=True,
        hawkes_params=(0.1, 0.3, 0.5),
        s2_event_layer=layer,
    )


def _ticks_1s(n_bars: int = 6, shock_every: int = 120):
    """A 1s z-series spanning n_bars 4H bars, with shocks sized to make separate
    excursions.

    Two constraints, both learned by getting them wrong: the shock must be small
    enough that C+ decays back under h within seconds (a large one pins C+ above
    h for hundreds of seconds and reads as ONE long excursion), and the spacing
    must be SHORTER than the 300s cluster cooldown, or every shock opens a fresh
    cluster and none is ever confirmed."""
    total = n_bars * BAR_S
    secs = np.arange(T0.timestamp(), T0.timestamp() + total, 1.0)
    z = np.zeros(len(secs))
    z[::shock_every] = 3.0
    return secs, z


def _continuous_events(secs, z):
    """Reference: one pass, no bar boundaries at all."""
    acc, layer, out = CUSUMShort(), S2EventLayer(), []
    for k in range(len(secs)):
        trig = acc.update(float(z[k]), float(secs[k]))
        if not trig.triggered:
            continue
        ev = layer.on_trigger(
            datetime.fromtimestamp(float(secs[k]), timezone.utc),
            trig.direction,
            max(trig.cusum_positive, trig.cusum_negative),
        )
        if ev is not None:
            out.append(ev)
    return out


def _engine_cursor_events(secs, z, n_bars):
    """Drive _advance_1s_channel with process_frame's own cursor discipline:
    prev starts None, then becomes each bar's close in turn."""
    eng = _engine(S2EventLayer())
    out, prev = [], None
    for i in range(n_bars):
        bar_unix = T0.timestamp() + (i + 1) * BAR_S
        out.extend(eng._advance_1s_channel((secs, z), prev, bar_unix))
        prev = bar_unix
    return out


def test_bar_cursor_matches_a_continuous_pass():
    n_bars = 6
    secs, z = _ticks_1s(n_bars)
    ref = _continuous_events(secs, z)
    got = _engine_cursor_events(secs, z, n_bars)
    assert ref, "fixture produced no events — it cannot prove anything"
    assert [(e.eval_ts, e.direction) for e in got] == [
        (e.eval_ts, e.direction) for e in ref
    ]


def test_no_second_is_processed_twice_across_a_boundary():
    """Double-counting would inflate excursion counts and confirm clusters early."""
    n_bars = 4
    secs, z = _ticks_1s(n_bars)
    got = _engine_cursor_events(secs, z, n_bars)
    stamps = [e.eval_ts for e in got]
    assert len(stamps) == len(set(stamps))


def test_a_shock_exactly_on_a_bar_boundary_is_not_lost():
    """The boundary second belongs to exactly one chunk. Off-by-one here silently
    drops whichever events land on bar closes."""
    n_bars = 5
    total = n_bars * BAR_S
    secs = np.arange(T0.timestamp(), T0.timestamp() + total, 1.0)
    z = np.zeros(len(secs))
    # two excursions straddling the first bar close, close enough to confirm
    boundary = BAR_S
    z[boundary - 30] = 3.0
    z[boundary] = 3.0
    z[boundary + 30] = 3.0
    ref = _continuous_events(secs, z)
    got = _engine_cursor_events(secs, z, n_bars)
    assert ref, "fixture produced no events across the boundary"
    assert [e.eval_ts for e in got] == [e.eval_ts for e in ref]


def test_cursor_starting_at_none_covers_the_first_bar():
    """prev_bar_unix is None on the first bar; treating that as 'skip' would lose
    every event before the first bar close."""
    secs, z = _ticks_1s(2)
    eng = _engine(S2EventLayer())
    first = eng._advance_1s_channel((secs, z), None, T0.timestamp() + BAR_S)
    # the fixture shocks every 120s, so the first 4H bar contains many
    assert first, "no events in the first bar — the None cursor swallowed them"


def test_absent_feed_yields_no_events_and_does_not_raise():
    eng = _engine(S2EventLayer())
    assert eng._advance_1s_channel(None, None, T0.timestamp()) == []


def test_absent_layer_yields_no_events():
    """Engine built without an injected layer must stay inert, not crash."""
    eng = _engine(None)
    secs, z = _ticks_1s(2)
    assert eng._advance_1s_channel((secs, z), None, T0.timestamp() + BAR_S) == []
