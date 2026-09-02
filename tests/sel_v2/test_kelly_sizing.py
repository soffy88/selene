"""Tests for sel_v2.strategies.kelly_sizing (v2.1 §7.1)."""

from datetime import datetime, timedelta, timezone

import pytest

from sel_v2.strategies.kelly_sizing import (
    _FIXED_BASE_SIZE,
    KellyPhase,
    KellySizer,
)

_NOW = datetime(2025, 6, 1, tzinfo=timezone.utc)


def _add_trades(sizer: KellySizer, n: int, pnl_pct: float = 0.02) -> None:
    for i in range(n):
        sizer.record_trade(
            closed_at=_NOW - timedelta(days=n - i),
            pnl_pct=pnl_pct,
        )


# ── Phase 0/1: fixed size ──────────────────────────────────────────────────────


def test_phase0_returns_fixed_size_strategy1():
    s = KellySizer(strategy="strategy_1", phase=KellyPhase.PHASE_0)
    diag = s.compute(nav_usdt=100_000, as_of=_NOW)
    assert diag.suggested_base_pct == _FIXED_BASE_SIZE["strategy_1"]


def test_phase0_returns_fixed_size_strategy2():
    s = KellySizer(strategy="strategy_2", phase=KellyPhase.PHASE_0)
    diag = s.compute(nav_usdt=100_000, as_of=_NOW)
    assert diag.suggested_base_pct == _FIXED_BASE_SIZE["strategy_2"]


def test_phase1_still_fixed_even_with_30_trades():
    s = KellySizer(strategy="strategy_1", phase=KellyPhase.PHASE_1)
    _add_trades(s, 30, pnl_pct=0.02)
    diag = s.compute(nav_usdt=100_000, as_of=_NOW)
    assert diag.suggested_base_pct == _FIXED_BASE_SIZE["strategy_1"]
    # But W/R should be computed for diagnostics
    assert diag.win_rate == 1.0  # all wins
    assert diag.kelly_fraction is not None


def test_phase1_computes_kelly_diagnostic():
    s = KellySizer(strategy="strategy_1", phase=KellyPhase.PHASE_1)
    # Mix of wins and losses
    for _ in range(15):
        s.record_trade(_NOW - timedelta(days=20), pnl_pct=0.03)
    for _ in range(15):
        s.record_trade(_NOW - timedelta(days=10), pnl_pct=-0.01)
    diag = s.compute(nav_usdt=100_000, as_of=_NOW)
    assert diag.phase == KellyPhase.PHASE_1
    assert diag.win_rate == pytest.approx(0.5, abs=0.01)
    assert diag.kelly_fraction is not None


# ── Phase 2: quarter Kelly ─────────────────────────────────────────────────────


def test_phase2_applies_quarter_kelly_cap():
    s = KellySizer(strategy="strategy_1", phase=KellyPhase.PHASE_2)
    # 80% win rate, R=3 → f* = (0.8*3 - 0.2) / 3 = (2.4 - 0.2) / 3 ≈ 0.733
    # quarter Kelly ≈ 0.183 → within [5%, 25%]
    for _ in range(24):
        s.record_trade(_NOW - timedelta(days=30), pnl_pct=0.03)
    for _ in range(6):
        s.record_trade(_NOW - timedelta(days=10), pnl_pct=-0.01)
    diag = s.compute(nav_usdt=100_000, as_of=_NOW)
    assert diag.phase == KellyPhase.PHASE_2
    assert 0.05 <= diag.suggested_base_pct <= 0.25


def test_phase2_negative_edge_falls_back_to_fixed():
    s = KellySizer(strategy="strategy_1", phase=KellyPhase.PHASE_2)
    # Losing strategy: all losses
    for _ in range(30):
        s.record_trade(_NOW - timedelta(days=10), pnl_pct=-0.02)
    diag = s.compute(nav_usdt=100_000, as_of=_NOW)
    assert diag.negative_edge is True
    assert diag.suggested_base_pct == _FIXED_BASE_SIZE["strategy_1"]


# ── Phase switch readiness ─────────────────────────────────────────────────────


def test_not_ready_for_phase2_below_30():
    s = KellySizer(strategy="strategy_1")
    _add_trades(s, 29)
    assert s.is_ready_for_phase2() is False


def test_ready_for_phase2_at_30():
    s = KellySizer(strategy="strategy_1")
    _add_trades(s, 30)
    assert s.is_ready_for_phase2() is True


# ── Rolling window eviction ────────────────────────────────────────────────────


def test_rolling_window_evicts_old_trades():
    s = KellySizer(strategy="strategy_2")  # 30-day window
    # Add 10 old trades (35 days ago — outside 30-day window)
    for _ in range(10):
        s.record_trade(_NOW - timedelta(days=35), pnl_pct=0.05)
    # Add 5 recent trades
    for _ in range(5):
        s.record_trade(_NOW - timedelta(days=5), pnl_pct=-0.01)
    diag = s.compute(nav_usdt=100_000, as_of=_NOW)
    # Only the 5 recent trades should be in window
    assert diag.sample_size == 5


def test_empty_history_returns_fixed_size():
    s = KellySizer(strategy="strategy_1", phase=KellyPhase.PHASE_2)
    diag = s.compute(nav_usdt=100_000, as_of=_NOW)
    assert diag.sample_size == 0
    assert diag.suggested_base_pct == _FIXED_BASE_SIZE["strategy_1"]
