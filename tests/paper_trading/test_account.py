"""Tests for AccountManager."""
import pytest
from datetime import datetime, timezone, timedelta

from decision.config import load_config
from paper_trading.account import AccountManager
from paper_trading.schema import AccountState, Position, PositionSide, SimulatedFill


_T = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
_HASH = "abc123"


@pytest.fixture(scope="module")
def risk_cfg():
    return load_config().risk


def _make_manager(nav: float = 10_000.0) -> AccountManager:
    return AccountManager(nav, load_config().risk)


def _make_fill(fee_usdt: float = 1.0, action: str = "open") -> SimulatedFill:
    return SimulatedFill(
        time=_T,
        symbol="BTCUSDT",
        side=PositionSide.LONG,
        action=action,
        requested_size_usdt=2000.0,
        fill_price=60_000.0,
        fill_size_usdt=2000.0,
        slippage_usdt=1.0,
        fee_usdt=fee_usdt,
        net_size_usdt=2000.0 - fee_usdt,
        config_hash=_HASH,
    )


def _make_position(unrealized: float = 0.0) -> Position:
    pos = Position(
        symbol="BTCUSDT",
        side=PositionSide.LONG,
        open_time=_T,
        open_price=60_000.0,
        size_usdt=2000.0,
        current_size_usdt=2000.0,
        unrealized_pnl_usdt=unrealized,
    )
    return pos


# ---------------------------------------------------------------------------

class TestApplyFill:
    def test_open_fill_deducts_fee(self):
        mgr = _make_manager(nav=10_000.0)
        fill = _make_fill(fee_usdt=1.5, action="open")
        mgr.apply_fill(fill)
        state = mgr.snapshot(_T, None)
        assert state.nav_usdt == pytest.approx(10_000.0 - 1.5)

    def test_close_fill_adds_realized_pnl(self):
        mgr = _make_manager(nav=10_000.0)
        fill = _make_fill(fee_usdt=1.0, action="close")
        mgr.apply_fill(fill, realized_pnl=100.0)
        state = mgr.snapshot(_T, None)
        # nav = 10000 - 1 (fee) + 100 (pnl)
        assert state.nav_usdt == pytest.approx(10_099.0)
        assert state.realized_pnl_usdt == pytest.approx(100.0)

    def test_consecutive_losses_increment(self):
        mgr = _make_manager()
        fill = _make_fill(fee_usdt=0.0)
        mgr.apply_fill(fill, realized_pnl=-50.0)
        mgr.apply_fill(fill, realized_pnl=-30.0)
        state = mgr.snapshot(_T, None)
        assert state.consecutive_losses == 2

    def test_consecutive_losses_reset_on_win(self):
        mgr = _make_manager()
        fill = _make_fill(fee_usdt=0.0)
        mgr.apply_fill(fill, realized_pnl=-50.0)
        mgr.apply_fill(fill, realized_pnl=-30.0)
        mgr.apply_fill(fill, realized_pnl=200.0)  # win resets counter
        state = mgr.snapshot(_T, None)
        assert state.consecutive_losses == 0

    def test_nav_unchanged_when_no_realized_pnl(self):
        mgr = _make_manager(10_000.0)
        fill = _make_fill(fee_usdt=0.0, action="open")
        mgr.apply_fill(fill)
        state = mgr.snapshot(_T, None)
        assert state.nav_usdt == pytest.approx(10_000.0)


class TestCheckRisk:
    def test_cascade_forces_close(self, risk_cfg):
        mgr = _make_manager()
        pos = _make_position()
        should_close, reason = mgr.check_risk("Cascade", _T, pos)
        assert should_close is True
        assert "Cascade" in reason

    def test_cascade_forces_close_without_position(self):
        mgr = _make_manager()
        should_close, reason = mgr.check_risk("Cascade", _T, None)
        assert should_close is True

    def test_no_risk_normal_state(self):
        mgr = _make_manager()
        pos = _make_position(unrealized=50.0)
        should_close, reason = mgr.check_risk("Surging_Up", _T, pos)
        assert should_close is False

    def test_single_trade_max_loss(self, risk_cfg):
        """Unrealized loss > max_loss_per_trade_pct × NAV → force close."""
        nav = 10_000.0
        mgr = _make_manager(nav=nav)
        # Loss that exceeds 3% of NAV (= 300)
        pos = _make_position(unrealized=-400.0)
        should_close, reason = mgr.check_risk("Surging_Up", _T, pos)
        assert should_close is True
        assert "max loss" in reason.lower() or "trade" in reason.lower()

    def test_daily_drawdown_pause(self, risk_cfg):
        """Burn through 5%+ of daily nav → pause triggered."""
        mgr = _make_manager(nav=10_000.0)
        # Simulate consecutive losses until daily drawdown > 5%
        fill = _make_fill(fee_usdt=0.0)
        mgr.apply_fill(fill, realized_pnl=-600.0)  # -6% of 10000 → exceeds 5%
        should_close, reason = mgr.check_risk("Drifting_Calm", _T, None)
        assert should_close is True
        assert "drawdown" in reason.lower()

    def test_consecutive_loss_stop(self, risk_cfg):
        """3 consecutive losses → pause and force close."""
        mgr = _make_manager(nav=10_000.0)
        fill = _make_fill(fee_usdt=0.0)
        # Apply 3 losing trades (small so no single-trade stop trigger)
        for _ in range(risk_cfg.consecutive_loss_stop):
            mgr.apply_fill(fill, realized_pnl=-10.0)
        should_close, reason = mgr.check_risk("Drifting_Calm", _T, None)
        assert should_close is True
        assert "consecutive" in reason.lower() or "loss" in reason.lower()

    def test_pause_does_not_force_close_existing(self, risk_cfg):
        """
        After pause is set, check_risk returns False (existing positions can
        close naturally — no forced close just because of pause).
        """
        mgr = _make_manager(nav=10_000.0)
        fill = _make_fill(fee_usdt=0.0)
        # Trigger pause via consecutive losses
        for _ in range(risk_cfg.consecutive_loss_stop):
            mgr.apply_fill(fill, realized_pnl=-10.0)
        # First call sets the pause
        mgr.check_risk("Drifting_Calm", _T, None)
        # Next bar — same time (still within pause), no new trigger
        pos = _make_position(unrealized=5.0)  # small positive — no stop
        # Now check again with a state that doesn't trigger any other rule
        should_close, _ = mgr.check_risk("Drifting_Calm", _T, pos)
        # Pause itself does NOT force close existing position
        assert should_close is False


class TestSnapshot:
    def test_snapshot_with_position(self):
        mgr = _make_manager(10_000.0)
        pos = _make_position(unrealized=150.0)
        state = mgr.snapshot(_T, pos)
        assert state.nav_usdt == pytest.approx(10_000.0)
        assert state.unrealized_pnl_usdt == pytest.approx(150.0)
        assert state.position_side == "long"
        assert state.position_size_usdt == pytest.approx(2000.0)

    def test_snapshot_no_position(self):
        mgr = _make_manager(10_000.0)
        state = mgr.snapshot(_T, None)
        assert state.position_side is None
        assert state.position_size_usdt == pytest.approx(0.0)
        assert state.unrealized_pnl_usdt == pytest.approx(0.0)

    def test_snapshot_is_paused_false_when_not_paused(self):
        mgr = _make_manager()
        state = mgr.snapshot(_T, None)
        assert state.is_paused is False
        assert state.pause_until is None


class TestResetDaily:
    def test_reset_clears_drawdown_baseline(self):
        mgr = _make_manager(10_000.0)
        fill = _make_fill(fee_usdt=0.0)
        mgr.apply_fill(fill, realized_pnl=-300.0)  # -3% drawdown
        state_before = mgr.snapshot(_T, None)
        assert state_before.daily_drawdown_pct > 0

        tomorrow = _T + timedelta(hours=24)
        mgr.reset_daily(tomorrow)
        state_after = mgr.snapshot(tomorrow, None)
        # After reset, drawdown is 0 (new baseline = current nav)
        assert state_after.daily_drawdown_pct == pytest.approx(0.0)

    def test_reset_clears_expired_pause(self):
        mgr = _make_manager(10_000.0)
        fill = _make_fill(fee_usdt=0.0)
        # Trigger pause
        for _ in range(load_config().risk.consecutive_loss_stop):
            mgr.apply_fill(fill, realized_pnl=-10.0)
        mgr.check_risk("Drifting_Calm", _T, None)

        tomorrow = _T + timedelta(hours=25)
        mgr.reset_daily(tomorrow)
        state = mgr.snapshot(tomorrow, None)
        assert state.is_paused is False
        assert state.pause_until is None
