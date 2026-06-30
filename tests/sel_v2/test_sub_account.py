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
    ONE_SIDE_COST_PCT,
    FUNDING_BAR_FACTOR,
    round_trip_cost_usdt,
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


# ── Transaction costs & funding (P0-4: paper fills are no longer free) ─────────

def test_close_charges_round_trip_cost():
    """A flat (entry==exit) close must lose exactly the round-trip fee+slippage,
    not break even, because real fills pay taker fee + spread both ways."""
    e = _engine()
    pos = _open(e.subaccount_1, price=50_000.0, size_pct=0.20, leverage=2.0)
    notional = pos.notional_usdt
    closed = e.subaccount_1.close_position(pos.id, 50_000.0, _NOW, "flat")
    expected_cost = round_trip_cost_usdt(notional)
    assert expected_cost > 0
    assert closed.pnl_usdt == pytest.approx(-expected_cost, rel=1e-9)
    # gross price move was zero, so pnl_pct stays 0 while pnl_usdt is the cost.
    assert closed.pnl_pct == pytest.approx(0.0, abs=1e-12)


def test_gross_profit_reduced_by_cost():
    e = _engine()
    pos = _open(e.subaccount_1, price=50_000.0, size_pct=0.20, leverage=1.0)
    notional = pos.notional_usdt
    closed = e.subaccount_1.close_position(pos.id, 55_000.0, _NOW, "tp")  # +10%
    gross = 0.10 * notional
    assert closed.pnl_usdt == pytest.approx(gross - round_trip_cost_usdt(notional), rel=1e-9)


def test_funding_cost_deducted_for_long():
    """A long that accrued positive funding pays it at exit."""
    e = _engine()
    pos = _open(e.subaccount_1, price=50_000.0, size_pct=0.20, leverage=1.0)
    notional = pos.notional_usdt
    fr = 0.0001
    pos.accrue_funding(fr)                     # one bar of funding
    expected_funding = fr * FUNDING_BAR_FACTOR * notional
    assert pos.accrued_funding_usdt == pytest.approx(expected_funding, rel=1e-12)
    closed = e.subaccount_1.close_position(pos.id, 50_000.0, _NOW, "flat")
    expected = -round_trip_cost_usdt(notional) - expected_funding
    assert closed.pnl_usdt == pytest.approx(expected, rel=1e-9)


def test_funding_sign_flips_for_short():
    """A short receives funding when the rate is positive (sign flips)."""
    long_pos = _open(_engine().subaccount_1, direction="LONG", leverage=1.0)
    short_pos = _open(_engine().subaccount_1, direction="SHORT", leverage=1.0)
    long_pos.accrue_funding(0.0002)
    short_pos.accrue_funding(0.0002)
    assert long_pos.accrued_funding_usdt > 0     # long pays
    assert short_pos.accrued_funding_usdt < 0    # short receives
    assert long_pos.accrued_funding_usdt == pytest.approx(-short_pos.accrued_funding_usdt)


def test_reduce_charges_proportional_cost_and_funding():
    e = _engine()
    pos = _open(e.subaccount_1, price=50_000.0, size_pct=0.20, leverage=1.0)
    pos.accrue_funding(0.0001)
    full_funding = pos.accrued_funding_usdt
    reduced_usdt = pos.size_usdt * 0.5
    closed = e.subaccount_1.reduce_position(
        pos.id, fraction=0.5, exit_price=50_000.0, exit_time=_NOW, exit_reason="half")
    # half the funding charged now, half stays on the remaining position
    assert pos.accrued_funding_usdt == pytest.approx(full_funding * 0.5, rel=1e-9)
    expected = -round_trip_cost_usdt(reduced_usdt * pos.leverage) - full_funding * 0.5
    assert closed.pnl_usdt == pytest.approx(expected, rel=1e-9)
