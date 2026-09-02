"""Tests for the calibration backfill (P1-7): TDA threshold persistence and the
calibrate_all orchestrator + deploy-verify helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from sel_v2.offline import calibrate_all
from sel_v2.offline.calibrate_all import EXPECTED_PARAM_KEYS
from sel_v2.offline.hawkes_calibration import _persist_to_db as hawkes_persist
from sel_v2.offline.tda_calibration import _persist_to_db as tda_persist
from sel_v2.strategies.params_loader import load_strategy_params

# ── in-memory asyncpg (real save -> load chain) ───────────────────────────────


class _InMemoryConn:
    def __init__(self, store):
        self._store = store

    async def executemany(self, query, args):
        for key, value in args:
            self._store[key] = value

    async def fetch(self, query, *args):
        if args:
            return [{"param_key": k, "param_value": self._store[k]} for k in args[0] if k in self._store]
        return [{"param_key": k, "param_value": v} for k, v in self._store.items()]

    async def close(self):
        pass


def _patch_inmemory(store):
    return patch("asyncpg.connect", AsyncMock(return_value=_InMemoryConn(store)))


# ── TDA persistence ───────────────────────────────────────────────────────────


def test_tda_persist_writes_live_p95_key():
    """_persist_to_db must write the exact key the live reader queries:
    load_strategy_params('tda1', ['l1_threshold_p95']) -> 'tda1_l1_threshold_p95'."""
    store: dict = {}
    result = {"l1_q90": 1.0e-4, "l1_q95": 2.0e-4, "l1_q97": 3.0e-4}
    dsn = "postgresql://t:t@localhost/t"
    with _patch_inmemory(store):
        persisted = tda_persist(result, db_url=dsn)
        # round-trips through the REAL loader (no mock of load_strategy_params)
        got = load_strategy_params("tda1", ["l1_threshold_p95"], db_url=dsn)

    assert persisted == {"l1_threshold_p90": 1.0e-4, "l1_threshold_p95": 2.0e-4, "l1_threshold_p97": 3.0e-4}
    assert set(store) == {"tda1_l1_threshold_p90", "tda1_l1_threshold_p95", "tda1_l1_threshold_p97"}
    assert got["l1_threshold_p95"] == pytest.approx(2.0e-4)


def test_tda_persist_skips_non_finite():
    store: dict = {}
    with _patch_inmemory(store):
        out = tda_persist(
            {"l1_q90": 1e-4, "l1_q95": float("nan"), "l1_q97": 3e-4}, db_url="postgresql://t:t@localhost/t"
        )
    assert out is None
    assert store == {}


# ── Hawkes degenerate-fit guard (surfaced by the real end-to-end run) ──────────


@pytest.mark.parametrize(
    "fit",
    [
        {"mu": 0.16, "alpha": 2.5e289, "beta": 0.0},  # beta=0 → kernel never decays
        {"mu": 0.1, "alpha": 50.0, "beta": 1.0},  # branching 50 → diverged MLE
        {"mu": 0.1, "alpha": 0.02, "beta": float("nan")},  # non-finite
    ],
)
def test_hawkes_persist_rejects_degenerate_fit(fit):
    store: dict = {}
    with _patch_inmemory(store):
        assert hawkes_persist(fit, db_url="postgresql://t:t@localhost/t") is None
    assert store == {}  # nothing poisons the live params


def test_hawkes_persist_accepts_subcritical_fit():
    store: dict = {}
    with _patch_inmemory(store):
        out = hawkes_persist({"mu": 0.09, "alpha": 0.024, "beta": 0.043}, db_url="postgresql://t:t@localhost/t")
    assert out["mu_ref"] == 0.09
    assert set(store) == {"h2_mu_ref", "h2_alpha_ref", "h2_beta_ref", "h2_branching_ratio_threshold"}


# ── Orchestrator ──────────────────────────────────────────────────────────────


def test_run_all_aggregates_and_propagates_persist(monkeypatch):
    captured = {}

    def fake_hawkes(*, data_path, persist, db_url, **kw):
        captured["hawkes_persist"] = persist
        return {"full_fit": {"mu": 0.1, "alpha": 0.02, "beta": 0.04, "branching_ratio": 0.5}}

    def fake_tda(*, data_path, persist, db_url, **kw):
        captured["tda_persist"] = persist
        return {"l1_q90": 1e-4, "l1_q95": 2e-4, "l1_q97": 3e-4}

    monkeypatch.setattr("sel_v2.offline.hawkes_calibration.run_hawkes_calibration", fake_hawkes)
    monkeypatch.setattr("sel_v2.offline.tda_calibration.run_tda_calibration", fake_tda)

    out = calibrate_all.run_all("bars.parquet", persist=False)

    assert captured == {"hawkes_persist": False, "tda_persist": False}
    assert out["hawkes"]["branching_ratio"] == 0.5
    assert out["tda"]["l1_q95"] == 2e-4
    assert out["persisted"] is False
    assert set(out["expected_param_keys"]) == set(EXPECTED_PARAM_KEYS)


def test_expected_keys_cover_live_readers():
    """The backfill must cover every key the live system reads from v2_strategy_params."""
    # hawkes_intensity.from_h2_reference -> h2_{mu,alpha,beta}_ref
    # replay.py -> h2_branching_ratio_threshold, tda1_l1_threshold_p95
    for k in ("h2_mu_ref", "h2_alpha_ref", "h2_beta_ref", "h2_branching_ratio_threshold", "tda1_l1_threshold_p95"):
        assert k in EXPECTED_PARAM_KEYS


# ── Deploy-verify + export helpers ────────────────────────────────────────────


class _FakeConn:
    def __init__(self, *, fetch_rows=None):
        self._rows = fetch_rows or []

    async def fetch(self, sql, *args):
        return self._rows


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        conn = self._conn

        class _Ctx:
            async def __aenter__(self_inner):
                return conn

            async def __aexit__(self_inner, *a):
                return False

        return _Ctx()


@pytest.mark.asyncio
async def test_verify_params_present_reports_missing():
    have = [{"param_key": k} for k in ("h2_mu_ref", "h2_alpha_ref", "h2_beta_ref", "h2_branching_ratio_threshold")]
    res = await calibrate_all.verify_params_present(_FakePool(_FakeConn(fetch_rows=have)))
    assert res["ok"] is False
    assert "tda1_l1_threshold_p95" in res["missing"]
    assert "h2_mu_ref" in res["present"]


@pytest.mark.asyncio
async def test_verify_params_present_all_ok():
    have = [{"param_key": k} for k in EXPECTED_PARAM_KEYS]
    res = await calibrate_all.verify_params_present(_FakePool(_FakeConn(fetch_rows=have)))
    assert res["ok"] is True
    assert res["missing"] == []


@pytest.mark.asyncio
async def test_export_bars_parquet(tmp_path):
    rows = [
        {
            "time": datetime(2026, 6, 1, h, tzinfo=timezone.utc),
            "open": 100.0 + h,
            "high": 101.0 + h,
            "low": 99.0 + h,
            "close": 100.5 + h,
            "volume": 10.0,
        }
        for h in (0, 4, 8)
    ]
    path = str(tmp_path / "bars.parquet")
    n = await calibrate_all.export_bars_parquet(_FakePool(_FakeConn(fetch_rows=rows)), path, symbol="BTC-USDT")
    assert n == 3
    import pandas as pd

    df = pd.read_parquet(path)
    assert list(df["close"]) == [100.5, 104.5, 108.5]
