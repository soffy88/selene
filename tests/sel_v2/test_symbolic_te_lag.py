"""Symbolic transfer-entropy lag-fix tests (optimization item #15).

Previously the source history window ignored `lag` (correct only at lag=1).
"""
import numpy as np

from sel_v2.offline.transfer_entropy import symbolic_te


def _series(n=400, seed=7):
    rng = np.random.default_rng(seed)
    return rng.standard_normal(n)


def test_lag_beyond_embedding_returns_zero():
    src = _series()
    tgt = _series(seed=8)
    # d=3, lag=5 → x_start = d-lag = -2 < 0 → guarded to 0.0
    assert symbolic_te(src, tgt, d=3, lag=5) == 0.0


def test_lagged_copy_detected_at_true_lag():
    # target[t] = source[t-2]: source transfers to target at lag 2.
    src = _series(n=600, seed=11)
    tgt = np.empty_like(src)
    tgt[2:] = src[:-2]
    tgt[:2] = src[:2]
    te_lag2 = symbolic_te(src, tgt, d=3, lag=2)
    te_lag1 = symbolic_te(src, tgt, d=3, lag=1)
    # The true lag (2) should capture more transfer than the wrong lag (1).
    assert te_lag2 > te_lag1
    assert te_lag2 > 0.0


def test_lag1_still_works():
    src = _series(seed=3)
    tgt = _series(seed=4)
    val = symbolic_te(src, tgt, d=3, lag=1)
    assert val >= 0.0
