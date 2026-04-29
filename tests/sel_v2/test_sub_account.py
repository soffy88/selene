"""Tests for sel_v2.strategies.sub_account (v2.0 §15)."""
import pytest
from datetime import datetime, timezone

from sel_v2.strategies.sub_account import (
    DualSubAccountEngine,
    SubAccount,
    SUBACCOUNT_1_FRACTION,
    SUBACCOUNT_2_FRACTION,
    MAX_CONCURRENT_S1,
    MAX_CONCURRENT_S2,
)

_NOW = datetime(2025, 6, 1, tzinfo=timezone.utc)
_NAV = 100_000.0


def _engine() -> DualSubAccountEngine:
    return DualSubAccountEngine(total_nav_usdt=_NAV)


def _open(account: SubAccount, price: float = 50_000.0, direction: str = "LONG",
          size_pct: float = 0.20, leverage: float = 1.0):
    return account.open_position(
        direction=direction,
        entry_price=price,
        size_pct=size_pct,
        leverage=leverage,
        instrument="BTC-USDT-SWAP",
        entry_time=_NOW,
        entry_state="Coiling",
    )


# ── Initial allocation ─────────────────────────────────────────────────────────

def test_initial_nav_allocation():
    e = _engine()
    assert e.subaccount_1.nav == pytest.approx(_NAV * SUBACCOUNT_1_FRACTION)
    assert e.subaccount_2.nav == pytest.approx(_NAV * SUBACCOUNT_2_FRACTION)


def test_total_equity_at_entry():
    e = _engine()
    assert e.total_equity(mark_price=50_000.0) == pytest.approx(_NAV)


# ── Max concurrent ─────────────────────────────────────────────────────────────

def test_strategy1_max_concurrent_1():
    e = _engine()
    p1 = _open(e.subaccount_1)
    assert p1 is not None
    p2 = _open(e.subaccount_1)
    assert p2 is None  # max_concurrent=1 reached


def test_strategy2_max_concurrent_2():
    e = _engine()
    p1 = _open(e.subaccount_2, size_pct=0.10)
    p2 = _open(e.subaccount_2, size_pct=0.10, direction="SHORT")
    p3 = _open(e.subaccount_2, size_pct=0.10)
    assert p1 is not None
    assert p2 is not None
    assert p3 is None  # max_concurrent=2 reached


# ── Open / close ──────────────────────────────────────────────────────────────

def test_close_profitable_position_updates_nav():
    e = _engine()
    initial_nav = e.subaccount_1.nav
    pos = _open(e.subaccount_1, price=50_000.0, size_pct=0.20)
    assert pos is not None

    closed = e.subaccount_1.close_position(
        pos.id, exit_price=55_000.0, exit_time=_NOW, exit_reason="test"
    )
    assert closed is not None
    assert closed.pnl_pct == pytest.approx(0.10, abs=1e-6)  # +10%
    assert e.subaccount_1.nav > initial_nav


def test_close_losing_position_reduces_nav():
    e = _engine()
    initial_nav = e.subaccount_1.nav
    pos = _open(e.subaccount_1, price=50_000.0, size_pct=0.20)

    closed = e.subaccount_1.close_position(
        pos.id, exit_price=48_500.0, exit_time=_NOW, exit_reason="drawdown"
    )
    assert closed is not None
    assert closed.pnl_pct < 0
    assert e.subaccount_1.nav < initial_nav


def test_close_nonexistent_returns_none():
    e = _engine()
    result = e.subaccount_1.close_position("nonexistent-id", 50_000.0, _NOW, "test")
    assert result is None


# ── Partial reduction ─────────────────────────────────────────────────────────

def test_reduce_50pct_position():
    e = _engine()
    pos = _open(e.subaccount_1, price=50_000.0, size_pct=0.20)
    assert pos is not None
    original_size = pos.size_usdt

    closed = e.subaccount_1.reduce_position(
        pos.id, fraction=0.50, exit_price=52_000.0,
        exit_time=_NOW, exit_reason="Critical reduce",
    )
    assert closed is not None
    assert pos.size_usdt == pytest.approx(original_size * 0.50, rel=1e-6)
    assert len(e.subaccount_1.open_positions) == 1  # still open, just smaller


# ── Cascade close all ─────────────────────────────────────────────────────────

def test_cascade_closes_all_strategy2_positions():
    e = _engine()
    p1 = _open(e.subaccount_2, size_pct=0.10)
    p2 = _open(e.subaccount_2, size_pct=0.10, direction="SHORT")
    assert len(e.subaccount_2.open_positions) == 2

    closed = e.subaccount_2.cascade_close_all(exit_price=50_000.0, exit_time=_NOW)
    assert len(closed) == 2
    assert len(e.subaccount_2.open_positions) == 0


def test_dual_engine_cascade_closes_both_accounts():
    e = _engine()
    _open(e.subaccount_1, size_pct=0.20)
    _open(e.subaccount_2, size_pct=0.10)

    closed = e.cascade_close_all(exit_price=49_000.0, exit_time=_NOW)
    assert len(closed) == 2
    assert len(e.subaccount_1.open_positions) == 0
    assert len(e.subaccount_2.open_positions) == 0


# ── Equity tracking ───────────────────────────────────────────────────────────

def test_unrealized_pnl_long():
    e = _engine()
    pos = _open(e.subaccount_1, price=50_000.0, size_pct=0.20)
    unrealized = e.subaccount_1.unrealized_pnl(mark_price=55_000.0)
    assert unrealized > 0


def test_unrealized_pnl_short():
    e = _engine()
    pos = _open(e.subaccount_1, price=50_000.0, size_pct=0.20, direction="SHORT")
    unrealized = e.subaccount_1.unrealized_pnl(mark_price=45_000.0)
    assert unrealized > 0


def test_closed_positions_recorded():
    e = _engine()
    pos = _open(e.subaccount_1)
    e.subaccount_1.close_position(pos.id, 52_000.0, _NOW, "test")
    assert len(e.subaccount_1.closed_positions) == 1
