"""LOB-depth percentile wiring (audit P1-3).

Cascade condition 1 (§5.7) is "σ > 97th pctile AND lob_depth < 5th pctile", but
lob_depth_pctile was never computed — it was permanently None, so the flagship extreme-event
defense's first condition could never fire. BarRunner now derives it (rolling rank of total
top-of-book depth) so a thin/exhausted book is observable.
"""

import numpy as np
import pandas as pd
import pytest

from sel_v2.scheduler.bar_runner import _SIGMA_WINDOW, BarRunner


def _runner(lob_depth_series=None, entropy_series=None, n=None):
    n = n or (_SIGMA_WINDOW + 60)
    closes = np.linspace(30000, 31000, n)
    sigma = np.full(n, 0.01)
    sigma_pctile = np.full(n, 0.5)
    nan = np.full(n, np.nan)
    df = pd.DataFrame(
        {
            "time": pd.to_datetime([pd.Timestamp("2024-01-01") + pd.Timedelta(hours=4 * i) for i in range(n)]),
            "close": closes,
        }
    )
    return BarRunner.from_precomputed(
        df=df,
        sigma_series=sigma,
        sigma_pctile_series=sigma_pctile,
        hawkes_br_series=nan,
        tda_l1_series=nan,
        tda_l1_pctile_series=nan,
        lob_depth_series=lob_depth_series,
        entropy_series=entropy_series,
    ), n


def test_thin_book_yields_low_depth_pctile():
    n = _SIGMA_WINDOW + 60
    depth = np.full(n, 1000.0)
    depth[-1] = 1.0  # final bar: book far thinner than its history
    runner, _ = _runner(depth)
    feat = runner.build_features(n - 1)
    assert feat.lob_depth_pctile is not None
    assert feat.lob_depth_pctile <= 0.05  # reachable for Cascade cond-1


def test_deep_book_yields_high_depth_pctile():
    n = _SIGMA_WINDOW + 60
    depth = np.full(n, 1000.0)
    depth[-1] = 5000.0  # final bar: book much deeper than history
    runner, _ = _runner(depth)
    feat = runner.build_features(n - 1)
    assert feat.lob_depth_pctile is not None
    assert feat.lob_depth_pctile >= 0.95


def test_none_series_leaves_pctile_none():
    runner, n = _runner(None)
    feat = runner.build_features(n - 1)
    assert feat.lob_depth_pctile is None  # no LOB data → conservative, cond stays None


# ── entropy wiring (follow-up B) ────────────────────────────────────────────


def test_entropy_pctile_populated_and_ranked():
    n = _SIGMA_WINDOW + 60
    ent = np.linspace(2.0, 3.0, n)  # rising entropy
    ent[-1] = 0.1  # final bar: entropy far below its history
    runner, _ = _runner(entropy_series=ent, n=n)
    feat = runner.build_features(n - 1)
    assert feat.entropy_4h == pytest.approx(0.1)
    assert feat.entropy_pctile is not None
    assert feat.entropy_pctile <= 0.05  # low entropy → low rank → Coiling's entropy_low can fire


def test_entropy_none_leaves_pctile_none():
    runner, n = _runner(entropy_series=None)
    feat = runner.build_features(n - 1)
    assert feat.entropy_pctile is None and feat.entropy_4h is None


# ── adaptive percentile window (A2) ─────────────────────────────────────────


def test_recently_started_feed_emits_pctile():
    """A feed that only has data in its recent tail (e.g. OI/funding/entropy collected for
    ~60 bars) must still produce a percentile, even though the window is far larger — the old
    range(window, n) emitted nothing and the state stayed permanently null."""
    n = _SIGMA_WINDOW + 60
    ent = np.full(n, np.nan)
    ent[-60:] = np.linspace(1.0, 2.0, 60)  # finite only in the last 60 bars
    runner, _ = _runner(entropy_series=ent, n=n)
    # _ENTROPY_PCTILE_WINDOW is 180 (> the 60 valid bars), but adaptive min_bars=30 emits.
    feat = runner.build_features(n - 1)
    assert feat.entropy_pctile is not None
    # too few valid points (< 30) → still None (don't fabricate a rank from noise)
    sparse = np.full(n, np.nan)
    sparse[-10:] = 1.0
    r2, _ = _runner(entropy_series=sparse, n=n)
    assert r2.build_features(n - 1).entropy_pctile is None


# ── entropy variance wiring (GL1 T0.1) ──────────────────────────────────────


def test_entropy_variance_populated_and_rising():
    """Entropy stable, then fluctuating for the final 3 bars → entropy_variance_rising
    should reach True end-to-end through BarRunner (proves the Critical A2 channel
    is live, not just that critical_logic's tristate math handles the field)."""
    n = _SIGMA_WINDOW + 60
    ent = np.full(n, 2.0)
    # last 8 bars: strictly widening oscillation so the rolling variance rises bar-over-bar
    for k, spread in enumerate([0.0, 0.0, 0.0, 0.0, 0.0, 0.1, 0.4, 1.0]):
        ent[n - 8 + k] = 2.0 + (spread if k % 2 else -spread)
    runner, _ = _runner(entropy_series=ent, n=n)
    feat = runner.build_features(n - 1)
    assert feat.entropy_variance is not None
    assert feat.entropy_variance_rising is True


def test_entropy_variance_none_when_series_none():
    runner, n = _runner(entropy_series=None)
    feat = runner.build_features(n - 1)
    assert feat.entropy_variance is None
    assert feat.entropy_variance_rising is None


def test_critical_a_full_reachable_end_to_end():
    """Construct a bar sequence where both A1 (sigma) and A2 (entropy_variance_rising)
    are real, wired conditions — proving Critical Path 1 (A_full AND (B OR C)) is
    reachable through the live BarRunner → critical_logic chain, not just through a
    hand-built BarFeatures (GL1 T0.1 acceptance #3)."""
    from sel_v2.states.critical_logic import evaluate_critical_entry

    n = _SIGMA_WINDOW + 60
    closes = np.linspace(30000, 31000, n)
    sigma = np.full(n, 0.01)
    sigma[-3:] = [0.02, 0.03, 0.04]  # strictly increasing → sigma_monotone_3bar=True
    sigma_pctile = np.full(n, 0.5)
    sigma_pctile[-1] = 0.95  # top-10% → A1 sigma_high
    nan = np.full(n, np.nan)
    hawkes_br = np.full(n, np.nan)
    hawkes_br[-1] = 0.90  # > default threshold 0.85 → B=True

    ent = np.full(n, 2.0)
    for k, spread in enumerate([0.0, 0.0, 0.0, 0.0, 0.0, 0.1, 0.4, 1.0]):
        ent[n - 8 + k] = 2.0 + (spread if k % 2 else -spread)

    df = pd.DataFrame(
        {
            "time": pd.to_datetime([pd.Timestamp("2024-01-01") + pd.Timedelta(hours=4 * i) for i in range(n)]),
            "close": closes,
        }
    )
    runner = BarRunner.from_precomputed(
        df=df,
        sigma_series=sigma,
        sigma_pctile_series=sigma_pctile,
        hawkes_br_series=hawkes_br,
        tda_l1_series=nan,
        tda_l1_pctile_series=nan,
        entropy_series=ent,
    )
    feat = runner.build_features(n - 1)
    assert feat.entropy_variance_rising is True
    cc = evaluate_critical_entry(feat)
    assert cc.a_full is True
    assert cc.path1_met is True
    assert cc.met is True
