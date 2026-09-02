"""Cost model + CPCV/PBO tests (optimization item #14)."""

import math

import pytest

from backtest.costs import (
    DEFAULT_TAKER_FEE,
    capacity_capped_notional,
    one_side_cost_pct,
    round_trip_cost_pct,
)
from backtest.cpcv import pbo_from_stats, pbo_proxy, run_cpcv


# ── cost model ──
def test_cost_without_adv_is_spread_plus_fee():
    # adv_usd=0 → no impact term; one-side = half_spread + taker_fee
    c = one_side_cost_pct(1000.0, 0.0)
    assert abs(c - ((2.0 / 10_000) / 2 + DEFAULT_TAKER_FEE)) < 1e-12


def test_impact_grows_with_size():
    small = one_side_cost_pct(1_000.0, 1_000_000.0)
    big = one_side_cost_pct(100_000.0, 1_000_000.0)
    assert big > small  # sqrt-impact increases with notional/ADV


def test_round_trip_is_double_one_side():
    assert abs(round_trip_cost_pct(5000.0, 1e6) - 2 * one_side_cost_pct(5000.0, 1e6)) < 1e-15


def test_maker_waives_fee():
    taker = one_side_cost_pct(1000.0, 0.0, is_maker=False)
    maker = one_side_cost_pct(1000.0, 0.0, is_maker=True)
    assert maker < taker


def test_capacity_caps_to_participation():
    # 50k notional, ADV 100k, 10% participation → capped at 10k
    assert capacity_capped_notional(50_000.0, 100_000.0, 0.10) == 10_000.0
    # under the cap → unchanged
    assert capacity_capped_notional(5_000.0, 100_000.0, 0.10) == 5_000.0
    # unknown ADV → unchanged
    assert capacity_capped_notional(50_000.0, 0.0) == 50_000.0


# ── PBO proxy ──
def test_pbo_proxy_all_negative():
    assert pbo_proxy([-0.1, -0.5, -2.0]) == 1.0


def test_pbo_proxy_all_positive():
    assert pbo_proxy([0.5, 1.0, 2.0]) == 0.0


def test_pbo_proxy_mixed_and_empty():
    assert pbo_proxy([1.0, -1.0, 2.0, -3.0]) == 0.5
    assert pbo_proxy([]) == 0.0


def test_pbo_from_stats_normal_approx():
    # mean well above 0 with small std → low PBO
    assert pbo_from_stats(2.0, 0.5) < 0.01
    # mean below 0 → high PBO
    assert pbo_from_stats(-1.0, 0.5) > 0.97
    # mean == 0 → 0.5
    assert abs(pbo_from_stats(0.0, 1.0) - 0.5) < 1e-9
    # degenerate std
    assert pbo_from_stats(1.0, 0.0) == 0.0
    assert pbo_from_stats(-1.0, 0.0) == 1.0


# ── CPCV integration (oskill installed in dev/CI) ──
def test_run_cpcv_returns_paths():
    pytest.importorskip("oskill")

    def bt(train_idx, test_idx):
        # mildly profitable, with variance so Sharpe is finite
        return [0.002 * math.sin(i) + 0.0008 for i in test_idx]

    res = run_cpcv(240, bt, n_folds=6, n_test_groups=2)
    assert res is not None
    assert res["n_paths"] >= 1
    assert 0.0 <= res["pbo"] <= 1.0
    assert res["median_sharpe"] is not None
