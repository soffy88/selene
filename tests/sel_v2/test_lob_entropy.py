"""Tests for sel_v2.features.lob_entropy (GL1 T0.1).

Entropy itself (Shannon entropy of the LOB) is computed upstream by iris
(shannon_entropy_topn, parity-verified against this repo's old calc_entropy
during the P3 collector-consolidation migration) — this module only computes
the *trend* of the already-collected entropy_4h series: rolling variance and
whether it's rising. So these tests exercise that with synthetic entropy_4h
sequences standing in for "order book distribution switching" (a uniform-book
period looks like a stable/flat entropy stretch; a period alternating between
concentrated and uniform books looks like a fluctuating entropy stretch) —
verifying variance responds in the correct direction, not the entropy formula
itself (that lives in iris, out of this repo's scope).
"""

import numpy as np
import pytest

from sel_v2.features.lob_entropy import (
    ENTROPY_VARIANCE_MIN_BARS,
    ENTROPY_VARIANCE_WINDOW_BARS,
    is_rising_3bar,
    rolling_entropy_variance,
)


# ── rolling_entropy_variance ────────────────────────────────────────────────


def test_stable_entropy_yields_low_variance():
    """A stable (uniform-book-like) entropy stretch → near-zero rolling variance."""
    n = 40
    entropy = np.full(n, 2.5)
    var = rolling_entropy_variance(entropy)
    assert var[-1] == pytest.approx(0.0, abs=1e-9)


def test_fluctuating_entropy_yields_higher_variance_than_stable():
    """A fluctuating (alternating concentrated/uniform book) entropy stretch has
    materially higher rolling variance than a stable stretch — direction check."""
    n = 40
    stable = np.full(n, 2.5)
    fluctuating = np.array([2.5 if i % 2 == 0 else 0.5 for i in range(n)])
    var_stable = rolling_entropy_variance(stable)
    var_fluct = rolling_entropy_variance(fluctuating)
    assert var_fluct[-1] > var_stable[-1]


def test_variance_direction_tracks_regime_switch():
    """Entropy stable for the first half, then starts fluctuating — variance should
    rise once the fluctuating regime enters the trailing window."""
    stable = np.full(ENTROPY_VARIANCE_WINDOW_BARS * 3, 2.0)
    fluctuating = np.array(
        [2.0 if i % 2 == 0 else 0.2 for i in range(ENTROPY_VARIANCE_WINDOW_BARS * 3)]
    )
    entropy = np.concatenate([stable, fluctuating])
    var = rolling_entropy_variance(entropy)
    # last bar's window is entirely inside the fluctuating regime → high variance
    assert var[-1] > 0.1
    # a bar still inside the stable regime → ~zero variance
    assert var[len(stable) - 1] == pytest.approx(0.0, abs=1e-9)


def test_below_min_bars_is_nan():
    n = ENTROPY_VARIANCE_MIN_BARS - 1
    entropy = np.linspace(1.0, 2.0, n)
    var = rolling_entropy_variance(entropy)
    assert np.all(np.isnan(var))


def test_at_min_bars_emits_value():
    n = ENTROPY_VARIANCE_MIN_BARS
    entropy = np.linspace(1.0, 2.0, n)
    var = rolling_entropy_variance(entropy)
    assert np.isfinite(var[-1])


def test_nan_gaps_excluded_from_window():
    entropy = np.array([2.0, np.nan, 2.0, 2.0, np.nan, 2.0])
    var = rolling_entropy_variance(entropy, window=6, min_bars=4)
    # 4 finite values (all == 2.0) in the window → variance 0, not NaN
    assert var[-1] == pytest.approx(0.0, abs=1e-9)


# ── is_rising_3bar ───────────────────────────────────────────────────────────


def test_strictly_increasing_is_true():
    series = np.array([1.0, 2.0, 3.0])
    assert is_rising_3bar(series, 2) is True


def test_flat_is_false():
    series = np.array([2.0, 2.0, 2.0])
    assert is_rising_3bar(series, 2) is False


def test_decreasing_is_false():
    series = np.array([3.0, 2.0, 1.0])
    assert is_rising_3bar(series, 2) is False


def test_insufficient_history_is_none():
    series = np.array([1.0, 2.0])
    assert is_rising_3bar(series, 1) is None


def test_nan_in_window_is_none():
    series = np.array([1.0, np.nan, 3.0])
    assert is_rising_3bar(series, 2) is None
