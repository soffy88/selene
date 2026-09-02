"""
Signal-edge measurement (P1-6): does a per-bar signal actually predict forward
returns?

The audit found zero validation of predictive power anywhere — no information
coefficient (IC), no hit-rate vs a random baseline. Tests proved the plumbing was
correct but never that a signal had edge. This module supplies that missing tool:
a rank IC (Spearman) and a sign hit-rate of a signal against k-bar-forward returns,
plus a convenience that measures the deployed engine's per-bar CUSUM signal.

Self-contained (numpy only); pointable at any signal series.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


def forward_returns(closes, horizon: int = 1) -> np.ndarray:
    """k-bar-ahead simple returns: (close[i+h] - close[i]) / close[i].
    The last `horizon` entries are NaN (no future bar)."""
    c = np.asarray(closes, dtype=float)
    out = np.full(len(c), np.nan)
    if horizon < 1 or len(c) <= horizon:
        return out
    out[:-horizon] = (c[horizon:] - c[:-horizon]) / c[:-horizon]
    return out


def _avg_rank(a: np.ndarray) -> np.ndarray:
    """Average ranks (1-based), ties share the mean rank — for Spearman."""
    n = len(a)
    order = np.argsort(a, kind="mergesort")
    sa = a[order]
    ranks = np.empty(n, dtype=float)
    i = 0
    while i < n:
        j = i
        while j + 1 < n and sa[j + 1] == sa[i]:
            j += 1
        ranks[order[i : j + 1]] = (i + j) / 2.0 + 1.0
        i = j + 1
    return ranks


def _spearman(x: np.ndarray, y: np.ndarray) -> float:
    if len(x) < 3:
        return 0.0
    rx, ry = _avg_rank(x), _avg_rank(y)
    if np.std(rx) == 0 or np.std(ry) == 0:
        return 0.0
    return float(np.corrcoef(rx, ry)[0, 1])


@dataclass
class SignalEdge:
    ic: float  # Spearman rank correlation of signal vs forward return
    hit_rate: float  # fraction of bars where sign(signal) == sign(fwd return)
    n: int  # number of usable (aligned, non-NaN, non-zero-signal) bars
    horizon: int


def evaluate_signal_edge(signal, closes, horizon: int = 1) -> SignalEdge:
    """Measure the predictive edge of `signal` for `horizon`-bar-forward returns.

    Pairs are dropped where either side is NaN; hit-rate ignores zero-signal bars
    (no directional call) and zero forward returns (no outcome)."""
    sig = np.asarray(signal, dtype=float)
    fwd = forward_returns(closes, horizon)
    n = min(len(sig), len(fwd))
    sig, fwd = sig[:n], fwd[:n]
    mask = ~(np.isnan(sig) | np.isnan(fwd))
    sig, fwd = sig[mask], fwd[mask]
    if len(sig) < 3:
        return SignalEdge(ic=0.0, hit_rate=0.0, n=len(sig), horizon=horizon)

    ic = _spearman(sig, fwd)

    dir_mask = (sig != 0) & (fwd != 0)
    if dir_mask.any():
        hits = np.sign(sig[dir_mask]) == np.sign(fwd[dir_mask])
        hit_rate = float(np.mean(hits))
    else:
        hit_rate = 0.0
    return SignalEdge(ic=ic, hit_rate=hit_rate, n=len(sig), horizon=horizon)


def engine_cusum_signal_edge(
    df, horizon: int = 1, *, hawkes_params=(0.1, 0.3, 0.5), oi_series=None, funding_series=None, ofi_series=None
) -> SignalEdge:
    """Run the deployed engine over `df` and measure the edge of its per-bar
    CUSUM-Short z-signal (the momentum trigger that drives both strategies)
    against forward returns. A measurement hook for real data — interpret the IC,
    don't assume it on synthetic input."""
    from sel_v2.paper.strategy_engine import PaperStrategyEngine

    engine = PaperStrategyEngine(skip_hawkes=True, skip_tda=True, hawkes_params=hawkes_params)
    signal = engine.bar_signal_series(df, oi_series=oi_series, funding_series=funding_series, ofi_series=ofi_series)
    closes = np.asarray(df["close"].values, dtype=float)
    return evaluate_signal_edge(signal, closes, horizon)
