"""Tests for the v2.2 lens observation tools (CHAN2/CHAN3/ICT2) + runner wiring."""

from datetime import datetime, timedelta, timezone

import numpy as np

from sel_v2.observation_tools.base import BarFeatures
from sel_v2.observation_tools.chan_tools import (
    MIN_BARS,
    ChanDivergence,
    ChanPivot,
)
from sel_v2.observation_tools.runner import (
    _LENS_TOOL_IDS,
    _TOOL_SOURCE_MAP,
    _VOCAB_MAP,
    run_recent_observations,
)
from sel_v2.observation_tools.swing_structure import SwingStructureTool

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _bar(i, close, high=None, low=None, state="Drifting_Calm", prev_close=None):
    prev = prev_close if prev_close is not None else close
    return BarFeatures(
        timestamp=T0 + timedelta(hours=4 * i),
        log_return=float(np.log(close / prev)) if prev > 0 else 0.0,
        volume=1.0,
        high=high if high is not None else close * 1.002,
        low=low if low is not None else close * 0.998,
        close=close,
        state=state,
    )


def _random_closes(n=200, seed=11):
    rng = np.random.default_rng(seed)
    return 100.0 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))


# ── warmup / three-state discipline ──────────────────────────────────────────


def test_lens_tools_not_ready_before_warmup_and_never_signal():
    closes = _random_closes()
    for tool in (ChanPivot(), ChanDivergence(), SwingStructureTool()):
        for i in range(MIN_BARS - 1):
            r = tool.update(_bar(i, closes[i], prev_close=closes[i - 1] if i else None))
            assert not tool.is_ready()
            assert r.signal is False and r.label == "WARMING"


def test_missing_prices_do_not_advance_state():
    closes = _random_closes()
    tool = ChanPivot()
    for i in range(MIN_BARS + 20):
        tool.update(_bar(i, closes[i]))
    n_before = len(tool._close)
    r = tool.update(
        BarFeatures(timestamp=T0, log_return=0.0, volume=1.0)  # no high/low/close
    )
    assert r.signal is False and r.label == "WARMING"
    assert len(tool._close) == n_before  # window did not advance


def test_chan_divergence_none_state_freezes_bookkeeping():
    closes = _random_closes()
    tool = ChanDivergence()
    for i in range(MIN_BARS + 5):
        tool.update(_bar(i, closes[i], state="Surging"))
    n_before = len(tool._close)
    r = tool.update(_bar(MIN_BARS + 5, closes[MIN_BARS + 5], state=None))
    assert r.signal is False
    assert len(tool._close) == n_before  # frozen, not advanced


def test_reset_restores_initial_state():
    closes = _random_closes()
    tool = SwingStructureTool()
    for i in range(MIN_BARS + 10):
        tool.update(_bar(i, closes[i]))
    assert tool.is_ready()
    tool.reset()
    assert not tool.is_ready()


# ── determinism / equivalence with the offline lens ──────────────────────────


def test_chan_pivot_matches_offline_series_on_same_window():
    from sel_v2.offline.chan_lens import pivot_overlap_series
    from sel_v2.offline.lens_common import compute_atr

    closes = _random_closes(300)
    highs, lows = closes * 1.002, closes * 0.998
    tool = ChanPivot()
    last = None
    for i in range(len(closes)):
        last = tool.update(_bar(i, closes[i], high=highs[i], low=lows[i]))
    offline = pivot_overlap_series(closes, compute_atr(highs, lows, closes))[-1]
    assert np.isfinite(offline)
    assert last.value == float(offline)  # one code path — identical math


# ── runner wiring ────────────────────────────────────────────────────────────


def test_vocab_and_source_maps_cover_lens_tools():
    assert _VOCAB_MAP["CHAN2"] == "chan_divergence"
    assert _VOCAB_MAP["CHAN3"] == "chan_pivot"
    assert _VOCAB_MAP["ICT2"] == "swing_structure"
    assert _TOOL_SOURCE_MAP["CHAN2"] == "chan"
    assert _TOOL_SOURCE_MAP["ICT2"] == "ict"
    assert _LENS_TOOL_IDS == {"CHAN2", "CHAN3", "ICT2", "ICT3"}
    assert _VOCAB_MAP["ICT3"] == "killzone_anomaly"
    assert "chan_retest" not in _VOCAB_MAP.values()  # CHAN-1 rejected offline


def test_run_recent_observations_fired_sink_collects_only_lens_tools():
    import pandas as pd

    closes = _random_closes(250, seed=5)
    df = pd.DataFrame(
        {
            "time": [T0 + timedelta(hours=4 * i) for i in range(len(closes))],
            "high": closes * 1.01,
            "low": closes * 0.99,
            "close": closes,
            "volume": np.ones(len(closes)),
        }
    )
    state_by_ts = {t: "Drifting_Calm" for t in df["time"]}
    fired: list = []
    results = run_recent_observations(df, state_by_ts=state_by_ts, fired_sink=fired)
    assert results  # latest readings returned as before
    assert {r.tool_id for r in results} >= {"CHAN2", "CHAN3", "ICT2", "ICT3"}
    assert all(f.tool_id in _LENS_TOOL_IDS for f in fired)
    assert all(f.signal for f in fired)
    # lens results carry the sel state for associated_state persistence
    lens_latest = [r for r in results if r.tool_id in _LENS_TOOL_IDS]
    assert all(
        r.metadata.get("associated_state") == "Drifting_Calm"
        for r in lens_latest
        if r.label != "WARMING"
    )


def test_run_recent_observations_without_new_kwargs_still_works():
    """Legacy call shape (no state_by_ts / fired_sink) must be unaffected."""
    import pandas as pd

    closes = _random_closes(100, seed=6)
    df = pd.DataFrame(
        {
            "time": [T0 + timedelta(hours=4 * i) for i in range(len(closes))],
            "close": closes,
            "volume": np.ones(len(closes)),
        }
    )
    results = run_recent_observations(df)
    assert isinstance(results, list)


def test_killzone_anomaly_session_adjusted():
    from sel_v2.observation_tools.killzone import MIN_SLOT_SAMPLES, KillzoneAnomaly

    tool = KillzoneAnomaly()
    # warm 35 days of 6 slots with tiny quiet bars (|ret| ~0.001, volume 1.0)
    rng = np.random.default_rng(9)
    i = 0
    for _d in range(MIN_SLOT_SAMPLES + 5):
        for _s in range(6):
            b = _bar(i, 100.0)
            b.log_return = float(rng.normal(0, 0.001))
            b.volume = 1.0 + float(rng.uniform(0, 0.1))
            r = tool.update(b)
            i += 1
    assert tool.is_ready()
    assert r.signal is False  # quiet bar in a quiet slot

    # a bar hugely abnormal FOR ITS SLOT fires
    b = _bar(i, 100.0)
    b.log_return = 0.05
    b.volume = 50.0
    r = tool.update(b)
    assert r.signal is True and r.label.endswith("_ANOMALY")
    assert r.metadata["ret_slot_pctile"] > 0.9
    assert 0 <= r.metadata["slot_utc"] <= 20
