"""Combinatorial Purged Cross-Validation (CPCV) + overfit probability (item #14).

Wraps oskill.cpcv_pipeline to produce the distribution of OOS path Sharpes a
single-path WFO replay cannot estimate, and derives a Probability-of-Backtest-
Overfitting proxy (the fraction of CPCV paths that are unprofitable OOS).
"""
from __future__ import annotations

import math
from typing import Callable, Optional


def pbo_proxy(path_sharpes) -> float:
    """Fraction of CPCV paths with Sharpe <= 0 — a simple overfit indicator.
    1.0 means every recombined OOS path lost money (almost certainly overfit);
    0.0 means all paths were profitable. Returns 0.0 for an empty input."""
    paths = [s for s in path_sharpes if s is not None]
    if not paths:
        return 0.0
    return sum(1 for s in paths if s <= 0) / len(paths)


def _normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def pbo_from_stats(mean: float, std: float) -> float:
    """PBO estimate P(path Sharpe <= 0) from the CPCV path-Sharpe distribution.
    Normal approximation; degenerate (std==0) collapses to a hard 0/1."""
    if std is None or std <= 0:
        return 1.0 if (mean is None or mean <= 0) else 0.0
    return _normal_cdf(-mean / std)


def run_cpcv(n_total: int, backtest_fn: Callable, *,
             n_folds: int = 6, n_test_groups: int = 2,
             embargo_pct: float = 0.01) -> Optional[dict]:
    """Run CPCV and return {path_sharpes, median_sharpe, n_paths, pbo}.

    backtest_fn(train_idx, test_idx) must return the per-bar return array for the
    test indices. Returns None if oskill is unavailable.
    """
    try:
        import oskill
    except Exception:
        return None
    res = oskill.cpcv_pipeline(
        n_total, n_folds=n_folds, n_test_groups=n_test_groups,
        embargo_pct=embargo_pct, backtest_fn=backtest_fn,
        compute_path_statistics=True,
    )
    # oskill returns a summary dict of the path-Sharpe distribution, not the raw list.
    dist = res.get("paths_sharpe_distribution") or {}
    return {
        "distribution": dist,
        "median_sharpe": res.get("median_sharpe"),
        "min_sharpe": res.get("min_sharpe"),
        "max_sharpe": res.get("max_sharpe"),
        "n_paths": res.get("n_paths"),
        "pbo": pbo_from_stats(dist.get("mean"), dist.get("std")),
    }
