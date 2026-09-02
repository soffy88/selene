"""
Correlation-aware portfolio risk analytics (Phase 5).

Replaces / augments the static hardcoded correlation groups in services/risk/main.py with
*dynamic* analytics computed from realized returns:

  - correlation_matrix / covariance_matrix : from per-symbol return series.
  - correlated_exposure : worst-case same-direction notional among assets whose realized
        correlation exceeds a threshold (dynamic replacement for hardcoded asset groups).
  - parametric_var_correlated : portfolio VaR from w'Σw — captures cross-asset correlation
        instead of treating positions as independent.
  - stress_test : apply scenario return shocks to current positions.
  - funding_adjusted_cost : fold perpetual funding into the Kelly `cost` term.

Pure Python (stdlib only) so it is unit-testable without numpy/scipy.
"""

from __future__ import annotations

import math

# funding_adjusted_cost lives in shared/ (used by the portfolio service too); re-exported here
# for backward compatibility with existing imports.
from shared.quant import FUNDING_HOURS, funding_adjusted_cost  # noqa: F401


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _aligned_returns(returns_by_symbol):
    """Truncate all symbols' return series to the shortest common length (tail-aligned)."""
    symbols = sorted(returns_by_symbol)
    if not symbols:
        return [], []
    n = min(len(returns_by_symbol[s]) for s in symbols)
    if n == 0:
        return symbols, [returns_by_symbol[s][:0] for s in symbols]
    series = [list(returns_by_symbol[s])[-n:] for s in symbols]
    return symbols, series


def covariance_matrix(returns_by_symbol):
    """Return (symbols, cov) where cov[i][j] is the sample covariance of returns. Per-bar units."""
    symbols, series = _aligned_returns(returns_by_symbol)
    k = len(symbols)
    if k == 0:
        return [], []
    n = len(series[0])
    means = [_mean(s) for s in series]
    cov = [[0.0] * k for _ in range(k)]
    if n < 2:
        return symbols, cov
    for i in range(k):
        for j in range(i, k):
            c = sum((series[i][t] - means[i]) * (series[j][t] - means[j]) for t in range(n)) / (n - 1)
            cov[i][j] = cov[j][i] = c
    return symbols, cov


def correlation_matrix(returns_by_symbol):
    """Return (symbols, corr) with Pearson correlations in [-1, 1]."""
    symbols, cov = covariance_matrix(returns_by_symbol)
    k = len(symbols)
    corr = [[0.0] * k for _ in range(k)]
    for i in range(k):
        for j in range(k):
            denom = math.sqrt(cov[i][i] * cov[j][j])
            corr[i][j] = cov[i][j] / denom if denom > 0 else (1.0 if i == j else 0.0)
    return symbols, corr


def correlated_exposure(positions: dict, returns_by_symbol: dict, threshold: float = 0.6):
    """Worst-case same-direction notional among correlated assets, as a fraction of total gross.

    `positions` maps symbol -> signed notional (positive long, negative short). For each anchor
    position we sum the notional of every position that (a) shares its sign and (b) has realized
    correlation >= threshold with it. The largest such cluster is the concentration the static
    hardcoded groups were trying to bound — but measured from data, not a fixed list.

    Returns {"max_fraction", "anchor", "cluster": [symbols], "gross"}.
    """
    gross = sum(abs(v) for v in positions.values())
    if gross <= 0:
        return {"max_fraction": 0.0, "anchor": None, "cluster": [], "gross": 0.0}
    symbols, corr = correlation_matrix(returns_by_symbol)
    idx = {s: i for i, s in enumerate(symbols)}
    best = {"max_fraction": 0.0, "anchor": None, "cluster": [], "gross": gross}
    for anchor, a_notional in positions.items():
        if a_notional == 0 or anchor not in idx:
            continue
        a_sign = 1 if a_notional > 0 else -1
        cluster, cluster_notional = [], 0.0
        for sym, notional in positions.items():
            if notional == 0 or sym not in idx:
                continue
            same_dir = (notional > 0) == (a_sign > 0)
            rho = corr[idx[anchor]][idx[sym]] if sym != anchor else 1.0
            if same_dir and rho >= threshold:
                cluster.append(sym)
                cluster_notional += abs(notional)
        frac = cluster_notional / gross
        if frac > best["max_fraction"]:
            best = {"max_fraction": round(frac, 6), "anchor": anchor, "cluster": sorted(cluster), "gross": gross}
    return best


def same_direction_correlated_exposure(
    candidate: str,
    cand_sign: int,
    cand_notional: float,
    positions: dict,
    returns_by_symbol: dict,
    threshold: float = 0.6,
):
    """Notional of the candidate order plus every *existing* same-direction position whose
    realized correlation with the candidate is >= threshold.

    Drop-in dynamic replacement for the static correlation-group exposure gate: the caller
    divides the result by equity and compares to the exposure cap. `cand_sign` is +1 (long/buy)
    or -1 (short/sell). `positions` maps symbol -> {"side", "notional"} (the open-position
    registry). Returns None when the candidate has no return history, signalling the caller to
    fall back to the static group check rather than wave the order through.
    """
    syms, corr = correlation_matrix(returns_by_symbol)
    idx = {s: i for i, s in enumerate(syms)}
    if candidate not in idx:
        return None
    total = float(cand_notional)
    for sym, pos in positions.items():
        pos_sign = 1 if str(pos.get("side", "LONG")).upper() in ("BUY", "LONG") else -1
        if pos_sign != cand_sign:
            continue
        if sym == candidate:  # candidate already has an open position
            total += float(pos.get("notional", 0.0))
            continue
        if sym not in idx:
            # a same-direction position has no return history → correlation is unknown.
            # We must not silently drop it (that would under-count exposure), so signal the
            # caller to fall back to the conservative static group check.
            return None
        if corr[idx[candidate]][idx[sym]] >= threshold:
            total += float(pos.get("notional", 0.0))
    return total


def parametric_var_correlated(positions: dict, returns_by_symbol: dict, confidence: float = 0.95):
    """Portfolio VaR (positive dollar loss) from w'Σw, capturing cross-asset correlation.

    `positions` maps symbol -> signed notional. VaR = z * sqrt(w' Σ w), Σ in per-bar return
    units, so the result is a per-bar VaR in the same currency as the notionals.
    """
    symbols, cov = covariance_matrix(returns_by_symbol)
    if not symbols:
        return 0.0
    {s: i for i, s in enumerate(symbols)}
    w = [float(positions.get(s, 0.0)) for s in symbols]
    if all(x == 0 for x in w):
        return 0.0
    # variance = w' Σ w
    var = 0.0
    for i in range(len(symbols)):
        for j in range(len(symbols)):
            var += w[i] * cov[i][j] * w[j]
    var = max(var, 0.0)
    z = _z_score(confidence)
    return z * math.sqrt(var)


def _z_score(confidence: float) -> float:
    """One-sided normal z for a few common confidence levels (avoids a scipy dependency)."""
    table = {0.90: 1.2816, 0.95: 1.6449, 0.975: 1.9600, 0.99: 2.3263, 0.995: 2.5758}
    # nearest tabulated level
    return table[min(table, key=lambda c: abs(c - confidence))]


def stress_test(positions: dict, scenarios: dict):
    """Apply scenario return shocks to current positions.

    `scenarios` maps scenario_name -> {symbol: shock_return}. PnL for a scenario is
    sum(signed_notional * shock). Symbols absent from a scenario are assumed unshocked.
    Returns {scenario_name: pnl}.
    """
    out = {}
    for name, shocks in scenarios.items():
        pnl = 0.0
        for sym, notional in positions.items():
            pnl += notional * float(shocks.get(sym, 0.0))
        out[name] = round(pnl, 6)
    return out
