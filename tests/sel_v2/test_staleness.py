"""Tests for sel_v2.runtime.staleness (GL1 T0.4) — one test group per matrix cell."""

from datetime import datetime, timedelta, timezone

import pytest

from sel_v2.runtime.staleness import (
    DEFAULT_THRESHOLDS_S,
    StalenessEnforcement,
    enforcement_for,
    is_bar_stale,
    is_stale,
)

_NOW = datetime(2026, 7, 8, 12, 0, 0, tzinfo=timezone.utc)


# ── is_stale: thresholds + None handling ────────────────────────────────────


@pytest.mark.parametrize("source,threshold", DEFAULT_THRESHOLDS_S.items())
def test_is_stale_default_thresholds(source, threshold):
    fresh = _NOW - timedelta(seconds=threshold - 1)
    stale = _NOW - timedelta(seconds=threshold + 1)
    assert is_stale(source, fresh, _NOW) is False
    assert is_stale(source, stale, _NOW) is True


def test_is_stale_never_seen_is_stale():
    assert is_stale("ticks", None, _NOW) is True


def test_is_stale_naive_datetime_treated_as_utc():
    naive_fresh = (_NOW - timedelta(seconds=10)).replace(tzinfo=None)
    assert is_stale("ticks", naive_fresh, _NOW) is False


def test_is_stale_custom_thresholds_override_default():
    ts = _NOW - timedelta(seconds=200)
    assert is_stale("ticks", ts, _NOW) is True  # > default 90s
    assert is_stale("ticks", ts, _NOW, thresholds={"ticks": 300.0}) is False


def test_is_stale_unknown_source_raises():
    with pytest.raises(ValueError):
        is_stale("bar_4h", _NOW, _NOW)  # bar_4h uses is_bar_stale, not is_stale


# ── is_bar_stale ─────────────────────────────────────────────────────────────


def test_bar_stale_none_is_stale():
    assert is_bar_stale(None, _NOW) is True


def test_bar_fresh_within_grace_of_current_boundary():
    # _NOW=12:00 -> boundary 12:00; latest bar open=08:00 (previous boundary) is
    # still fresh because we're inside the grace window for the 12:00 bar to arrive.
    latest = _NOW.replace(hour=8, minute=0, second=0, microsecond=0)
    assert is_bar_stale(latest, _NOW) is False


def test_bar_stale_missing_previous_boundary_past_grace():
    # _NOW=12:20 is past the 15-min grace for the 12:00 boundary, so the 08:00 bar
    # (previous-previous boundary) is now stale — we should already have 12:00.
    now = _NOW.replace(hour=12, minute=20)
    latest = _NOW.replace(hour=8, minute=0, second=0, microsecond=0)
    assert is_bar_stale(latest, now) is True


def test_bar_fresh_when_latest_boundary_covered():
    now = _NOW.replace(hour=12, minute=20)
    latest = _NOW.replace(hour=12, minute=0, second=0, microsecond=0)
    assert is_bar_stale(latest, now) is False


# ── enforcement_for: the matrix itself ──────────────────────────────────────


def test_fresh_source_has_no_enforcement():
    for source in ("ticks", "funding_oi", "bar_4h", "lob"):
        e = enforcement_for(source, stale=False)
        assert e == StalenessEnforcement(source=source, stale=False)


def test_ticks_stale_blocks_s2_and_pauses_cusum_reversal_only():
    e = enforcement_for("ticks", stale=True)
    assert e.reason_code == "STALE_TICKS"
    assert e.block_s2_entry is True
    assert e.block_s1_entry is False  # S1 gating is funding_oi's job, not ticks'
    assert e.pause_cusum_reversal_exit is True
    assert e.skip_bar is False and e.entropy_none is False


def test_funding_oi_stale_blocks_s1_only():
    e = enforcement_for("funding_oi", stale=True)
    assert e.reason_code == "STALE_FUNDING_OI"
    assert e.block_s1_entry is True
    assert e.block_s2_entry is False
    assert e.pause_cusum_reversal_exit is False


def test_bar_4h_stale_skips_bar_only():
    e = enforcement_for("bar_4h", stale=True)
    assert e.reason_code == "STALE_BAR"
    assert e.skip_bar is True
    assert e.block_s1_entry is False and e.block_s2_entry is False


def test_lob_stale_degrades_entropy_only():
    e = enforcement_for("lob", stale=True)
    assert e.reason_code == "STALE_LOB"
    assert e.entropy_none is True
    assert e.block_s1_entry is False and e.block_s2_entry is False
    assert e.pause_cusum_reversal_exit is False


def test_enforcement_for_unknown_source_raises():
    with pytest.raises(ValueError):
        enforcement_for("nonsense", stale=True)
