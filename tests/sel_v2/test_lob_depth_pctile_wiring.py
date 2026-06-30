"""LOB-depth percentile wiring (audit P1-3).

Cascade condition 1 (§5.7) is "σ > 97th pctile AND lob_depth < 5th pctile", but
lob_depth_pctile was never computed — it was permanently None, so the flagship extreme-event
defense's first condition could never fire. BarRunner now derives it (rolling rank of total
top-of-book depth) so a thin/exhausted book is observable.
"""
import numpy as np
import pandas as pd

from sel_v2.scheduler.bar_runner import BarRunner, _SIGMA_WINDOW


def _runner(lob_depth_series):
    n = _SIGMA_WINDOW + 60
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
        lob_depth_series=lob_depth_series), n


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
