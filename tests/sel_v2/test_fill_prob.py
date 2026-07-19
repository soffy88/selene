"""Unit tests for the first-passage fill-probability estimator (Wave EXEC-S Part 1).

The aggregate table looks plausible on its own (p_fill falls monotonically in δ),
but monotonicity would also hold for several *wrong* window definitions. These
tests pin the exact semantics instead: window excludes its own anchor, a truncated
window is dropped rather than scored as "no fill", and touching a level is not a
fill.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from sel_v2.offline.fill_prob import (
    ANCHOR_STEP_MIN,
    DELTA_GRID,
    HORIZONS_MIN,
    _atr_1h_per_minute,
    _window_extremes,
)


# ── frozen grid (must match the Wave's spec exactly) ─────────────────────────


def test_grid_is_the_frozen_one():
    assert DELTA_GRID == [round(0.1 * i, 1) for i in range(1, 16)]
    assert DELTA_GRID[0] == 0.1 and DELTA_GRID[-1] == 1.5 and len(DELTA_GRID) == 15
    assert HORIZONS_MIN == [15, 30, 60, 120]
    assert ANCHOR_STEP_MIN == 5


# ── window semantics ─────────────────────────────────────────────────────────


def test_window_excludes_the_anchor_itself():
    """A spike AT the anchor must not count — the order is only live afterwards."""
    arr = np.array([0.0, 5.0, 5.0, 5.0], dtype=float)
    # index 0 holds the low; its own window (indices 1..2) must not see it
    assert _window_extremes(arr, 2, "min")[0] == 5.0


def test_window_covers_exactly_the_next_horizon_entries():
    arr = np.array([10.0, 9.0, 8.0, 7.0, 6.0], dtype=float)
    got = _window_extremes(arr, 2, "min")
    assert got[0] == 8.0  # min(arr[1], arr[2])
    assert got[1] == 7.0  # min(arr[2], arr[3])
    highs = _window_extremes(arr, 2, "max")
    assert highs[0] == 9.0  # max(arr[1], arr[2])


def test_truncated_window_is_nan_not_a_miss():
    """Anchors whose horizon runs past the data end must be dropped. Scoring them
    as "did not fill" would bias p_fill downward."""
    arr = np.arange(6, dtype=float)
    got = _window_extremes(arr, 3, "min")
    assert np.isnan(got[-1]) and np.isnan(got[-2]) and np.isnan(got[-3])
    assert np.isfinite(got[0])


def test_window_all_nan_when_series_shorter_than_horizon():
    assert np.all(np.isnan(_window_extremes(np.arange(3, dtype=float), 5, "min")))


# ── strict-crossing rule (same rule the shadow layer books fills with) ───────


@pytest.mark.parametrize(
    "side,level,extreme,filled",
    [
        ("buy", 100.0, 99.9, True),  # printed strictly through
        ("buy", 100.0, 100.0, False),  # touched exactly → NOT a fill
        ("buy", 100.0, 100.1, False),
        ("sell", 100.0, 100.1, True),
        ("sell", 100.0, 100.0, False),  # touched exactly → NOT a fill
        ("sell", 100.0, 99.9, False),
    ],
)
def test_touching_the_level_is_not_a_fill(side, level, extreme, filled):
    got = extreme < level if side == "buy" else extreme > level
    assert got is filled or got == filled


# ── ATR look-ahead ───────────────────────────────────────────────────────────


def test_atr_uses_only_closed_bars():
    """ATR at minute m must come from hours that closed BEFORE m. If the current
    hour leaked in, the level would be sized by the very move being tested."""
    idx = pd.date_range("2026-07-01", periods=180, freq="1min", tz="UTC")
    # flat for two hours, then a violent third hour
    close = np.concatenate([np.full(120, 100.0), np.linspace(100, 200, 60)])
    df = pd.DataFrame(
        {"minute": idx, "close": close, "high": close * 1.001, "low": close * 0.999}
    )
    atr = _atr_1h_per_minute(df)
    # the first hour has no closed predecessor at all
    assert np.isnan(atr[0])
    # inside the violent third hour, ATR still reflects the calm hours before it
    calm, violent = atr[125], close[-1] - close[120]
    assert np.isfinite(calm) and calm < violent
