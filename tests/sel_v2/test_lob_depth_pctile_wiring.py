"""LOB-depth percentile wiring (audit P1-3).

Cascade condition 1 (§5.7) is "σ > 97th pctile AND lob_depth < 5th pctile", but
lob_depth_pctile was never computed — it was permanently None, so the flagship extreme-event
defense's first condition could never fire. BarRunner now derives it (rolling rank of total
top-of-book depth) so a thin/exhausted book is observable.
"""
import numpy as np
import pandas as pd
import pytest

from sel_v2.scheduler.bar_runner import BarRunner, _SIGMA_WINDOW


def _runner(lob_depth_series=None, entropy_series=None, n=None):
    n = n or (_SIGMA_WINDOW + 60)
    closes = np.linspace(30000, 31000, n)
    sigma = np.full(n, 0.01)
    sigma_pctile = np.full(n, 0.5)
    nan = np.full(n, np.nan)
    df = pd.DataFrame({"time": pd.to_datetime(
        [pd.Timestamp("2024-01-01") + pd.Timedelta(hours=4 * i) for i in range(n)]),
        "close": closes})
    return BarRunner.from_precomputed(
        df=df, sigma_series=sigma, sigma_pctile_series=sigma_pctile,
        hawkes_br_series=nan, tda_l1_series=nan, tda_l1_pctile_series=nan,
        lob_depth_series=lob_depth_series, entropy_series=entropy_series), n


def test_thin_book_yields_low_depth_pctile():
    n = _SIGMA_WINDOW + 60
    depth = np.full(n, 1000.0)
    depth[-1] = 1.0          # final bar: book far thinner than its history
    runner, _ = _runner(depth)
    feat = runner.build_features(n - 1)
    assert feat.lob_depth_pctile is not None
    assert feat.lob_depth_pctile <= 0.05   # reachable for Cascade cond-1


def test_deep_book_yields_high_depth_pctile():
    n = _SIGMA_WINDOW + 60
    depth = np.full(n, 1000.0)
    depth[-1] = 5000.0       # final bar: book much deeper than history
    runner, _ = _runner(depth)
    feat = runner.build_features(n - 1)
    assert feat.lob_depth_pctile is not None
    assert feat.lob_depth_pctile >= 0.95


def test_none_series_leaves_pctile_none():
    runner, n = _runner(None)
    feat = runner.build_features(n - 1)
    assert feat.lob_depth_pctile is None   # no LOB data → conservative, cond stays None


# ── entropy wiring (follow-up B) ────────────────────────────────────────────

def test_entropy_pctile_populated_and_ranked():
    n = _SIGMA_WINDOW + 60
    ent = np.linspace(2.0, 3.0, n)       # rising entropy
    ent[-1] = 0.1                        # final bar: entropy far below its history
    runner, _ = _runner(entropy_series=ent, n=n)
    feat = runner.build_features(n - 1)
    assert feat.entropy_4h == pytest.approx(0.1)
    assert feat.entropy_pctile is not None
    assert feat.entropy_pctile <= 0.05   # low entropy → low rank → Coiling's entropy_low can fire


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
    ent[-60:] = np.linspace(1.0, 2.0, 60)   # finite only in the last 60 bars
    runner, _ = _runner(entropy_series=ent, n=n)
    # _ENTROPY_PCTILE_WINDOW is 180 (> the 60 valid bars), but adaptive min_bars=30 emits.
    feat = runner.build_features(n - 1)
    assert feat.entropy_pctile is not None
    # too few valid points (< 30) → still None (don't fabricate a rank from noise)
    sparse = np.full(n, np.nan); sparse[-10:] = 1.0
    r2, _ = _runner(entropy_series=sparse, n=n)
    assert r2.build_features(n - 1).entropy_pctile is None
