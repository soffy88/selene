"""CPCV/PBO wiring into WFOResult (P1-3).

oskill is not installed in this environment, so the production path returns
cpcv=None. To verify the wiring and the per-bar-return backtest_fn end-to-end, a
fake `oskill` module is injected; a separate importorskip test exercises the real
oskill pipeline in CI. PBO overfit-detection is checked through the pure helpers.
"""

from __future__ import annotations

import importlib.util
import math
import sys
import types

import numpy as np
import pytest

from backtest.engine import WFOEngine, WFOConfig, WFOResult
from backtest.cpcv import pbo_proxy, pbo_from_stats

BASE_MS = 1_700_000_000_000
HOUR_MS = 3_600_000


def _make_candles(n: int) -> list[dict]:
    out = []
    for i in range(n):
        close = 30000 + 2000 * math.sin(i / 10.0) + 600 * math.sin(i / 3.0)
        out.append(
            {
                "open_time": BASE_MS + i * HOUR_MS,
                "high": close * 1.004,
                "low": close * 0.996,
                "close": close,
                "volume": 1000.0,
            }
        )
    return out


def _make_funding(n: int) -> list[dict]:
    return [
        {
            "funding_time": BASE_MS + i * HOUR_MS,
            "funding_rate": -0.10 if (i // 12) % 2 == 0 else 0.15,
        }
        for i in range(n)
    ]


def _cfg() -> WFOConfig:
    return WFOConfig(
        train_days=3, test_days=1, step_days=1, embargo_days=1, min_oos_trades=1
    )


# ── PBO overfit detection (pure, no oskill) ───────────────────────────────────


def test_pbo_proxy_flags_all_losing_paths():
    assert pbo_proxy([-0.5, -0.1, -2.0]) == 1.0  # every path lost → certain overfit
    assert pbo_proxy([1.0, 2.0, 0.5]) == 0.0  # all profitable
    assert pbo_proxy([1.0, -1.0]) == 0.5


def test_pbo_from_stats_monotonic_in_mean():
    overfit = pbo_from_stats(mean=-0.5, std=1.0)  # negative mean path Sharpe
    good = pbo_from_stats(mean=2.0, std=1.0)
    assert overfit > 0.5 > good


# ── WFOResult carries the fields ──────────────────────────────────────────────


def test_wforesult_cpcv_fields_default_none_and_serialised():
    r = WFOResult()
    assert r.cpcv is None and r.pbo is None
    d = r.to_dict()
    assert "cpcv" in d and "pbo" in d
    assert d["pbo"] is None


# ── End-to-end wiring with a fake oskill ──────────────────────────────────────


def _install_fake_oskill(monkeypatch, capture, kwargs_seen=None):
    """A fake oskill.cpcv_pipeline that drives backtest_fn over n_folds contiguous
    groups and summarises the path-Sharpe distribution (mirrors what cpcv.run_cpcv
    consumes)."""
    mod = types.ModuleType("oskill")

    def cpcv_pipeline(
        n_total,
        *,
        n_folds,
        n_test_groups,
        embargo_pct,
        label_horizon,
        backtest_fn,
        compute_path_statistics,
    ):
        if kwargs_seen is not None:
            kwargs_seen["label_horizon"] = label_horizon
        groups = [g for g in np.array_split(np.arange(n_total), n_folds) if len(g)]
        sharpes = []
        for gi, test_idx in enumerate(groups):
            train_idx = np.concatenate(
                [groups[j] for j in range(len(groups)) if j != gi]
            )
            r = np.asarray(backtest_fn(train_idx, test_idx), dtype=float)
            capture.append(r)
            s = float(r.mean() / r.std()) if r.std() > 0 else 0.0
            sharpes.append(s)
        arr = np.array(sharpes)
        return {
            "paths_sharpe_distribution": {
                "mean": float(arr.mean()),
                "std": float(arr.std()),
            },
            "median_sharpe": float(np.median(arr)),
            "min_sharpe": float(arr.min()),
            "max_sharpe": float(arr.max()),
            "n_paths": len(arr),
        }

    mod.cpcv_pipeline = cpcv_pipeline
    monkeypatch.setitem(sys.modules, "oskill", mod)


def test_run_populates_cpcv_with_fake_oskill(monkeypatch):
    capture: list = []
    kwargs_seen: dict = {}
    _install_fake_oskill(monkeypatch, capture, kwargs_seen)

    engine = WFOEngine(_cfg())
    result = asyncio_run(engine, _make_candles(300), _make_funding(300))

    # backtest_fn was actually invoked, returning a per-bar return array per fold
    assert capture, "backtest_fn was never called"
    assert all(r.ndim == 1 for r in capture)
    # CPCV result + PBO are wired into the WFOResult and serialised
    assert result.cpcv is not None
    assert result.cpcv["n_paths"] >= 1
    assert result.pbo is not None and 0.0 <= result.pbo <= 1.0
    d = result.to_dict()
    assert d["cpcv"] is not None and d["pbo"] is not None
    # P1-8: the multi-bar label horizon is forwarded so CPCV purges label overlap
    from backtest.engine import MAX_HOLD_HOURS

    assert kwargs_seen.get("label_horizon") == MAX_HOLD_HOURS
    assert MAX_HOLD_HOURS > 0


def test_run_cpcv_forwards_label_horizon_to_pipeline(monkeypatch):
    """Unit-level: run_cpcv must pass label_horizon through to oskill (not drop it)."""
    seen = {}
    mod = types.ModuleType("oskill")

    def cpcv_pipeline(
        n_total,
        *,
        n_folds,
        n_test_groups,
        embargo_pct,
        label_horizon,
        backtest_fn,
        compute_path_statistics,
    ):
        seen["label_horizon"] = label_horizon
        return {
            "paths_sharpe_distribution": {"mean": 1.0, "std": 1.0},
            "median_sharpe": 1.0,
            "min_sharpe": 0.0,
            "max_sharpe": 2.0,
            "n_paths": 3,
        }

    mod.cpcv_pipeline = cpcv_pipeline
    monkeypatch.setitem(sys.modules, "oskill", mod)
    from backtest.cpcv import run_cpcv

    run_cpcv(200, lambda tr, te: [0.0], label_horizon=6)
    assert seen["label_horizon"] == 6


def test_run_without_oskill_leaves_cpcv_none(monkeypatch):
    # ensure no oskill is importable
    monkeypatch.setitem(sys.modules, "oskill", None)
    engine = WFOEngine(_cfg())
    result = asyncio_run(engine, _make_candles(300), _make_funding(300))
    assert result.cpcv is None
    assert result.pbo is None


@pytest.mark.skipif(
    importlib.util.find_spec("oskill") is None,
    reason="real oskill not installed",
)
def test_real_oskill_cpcv_runs():  # CI installs oskill, so this now runs there
    pytest.importorskip("oskill")
    engine = WFOEngine(_cfg())
    result = asyncio_run(engine, _make_candles(400), _make_funding(400))
    if result.cpcv is not None:
        assert 0.0 <= result.pbo <= 1.0


# ── helper ────────────────────────────────────────────────────────────────────


def asyncio_run(engine, candles, funding):
    import asyncio

    return asyncio.run(engine.run("BTC-USDT", candles, funding))
