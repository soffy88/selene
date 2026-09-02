"""Replay precompute cache (#4 perf).

The full-history replay runs on every tick, but σ/Hawkes-BR/TDA-L¹ are pure functions of the
close series and only change when a new bar seals. The cache must (a) return identical results
and (b) skip the expensive recompute when the closes are unchanged, then recompute when a bar
is appended.
"""

import numpy as np
import pandas as pd

import sel_v2.paper.strategy_engine as se_mod
from sel_v2.paper.strategy_engine import PaperStrategyEngine


def _df(n, seed=0):
    rng = np.random.default_rng(seed)
    close = 30000 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    t0 = pd.Timestamp("2024-01-01")
    return pd.DataFrame(
        {
            "time": [t0 + pd.Timedelta(hours=4 * i) for i in range(n)],
            "close": close,
            "open": close,
            "high": close * 1.002,
            "low": close * 0.998,
            "volume": 1000.0,
        }
    )


def _fresh_engine():
    se_mod._PRECOMPUTE_CACHE.update({"sig": None, "data": None})
    return PaperStrategyEngine(total_nav_usdt=100000, skip_tda=True, hawkes_params=(0.1, 0.3, 0.5))


def test_cache_hit_skips_recompute(monkeypatch):
    eng = _fresh_engine()
    calls = {"n": 0}
    real = se_mod.precompute_sigma_series

    def counting(closes):
        calls["n"] += 1
        return real(closes)

    monkeypatch.setattr(se_mod, "precompute_sigma_series", counting)

    closes = _df(220)["close"].values.astype(float)
    a = eng._precompute_price_features(closes)
    b = eng._precompute_price_features(closes.copy())  # same content → cache hit
    assert calls["n"] == 1  # only computed once
    for x, y in zip(a, b, strict=False):
        assert np.allclose(x, y, equal_nan=True)


def test_new_bar_invalidates_cache(monkeypatch):
    eng = _fresh_engine()
    calls = {"n": 0}
    real = se_mod.precompute_sigma_series
    monkeypatch.setattr(
        se_mod, "precompute_sigma_series", lambda c: (calls.__setitem__("n", calls["n"] + 1) or real(c))
    )
    closes = _df(220)["close"].values.astype(float)
    eng._precompute_price_features(closes)
    eng._precompute_price_features(np.append(closes, closes[-1] * 1.01))  # one new bar
    assert calls["n"] == 2  # recomputed on the new bar


def test_cached_result_matches_uncached():
    # The engine output must be unchanged by caching: run twice, identical states.
    eng = _fresh_engine()
    df = _df(560, seed=11)
    s1 = eng.process_frame(df)
    eng2 = _fresh_engine()
    s2 = eng2.process_frame(df)
    assert s1["state_counts"] == s2["state_counts"]
