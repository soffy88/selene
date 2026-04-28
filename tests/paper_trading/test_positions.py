"""Tests for PositionTracker."""
import pytest
from datetime import datetime, timezone

from paper_trading.fills import FillSimulator
from paper_trading.positions import PositionTracker
from paper_trading.schema import Position, PositionSide, SimulatedFill
from decision.config import load_config

_T = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
_HASH = "abc123"


@pytest.fixture
def sim():
    return FillSimulator(load_config().execution)


@pytest.fixture
def tracker():
    return PositionTracker()


def _make_fill(
    side: PositionSide = PositionSide.LONG,
    fill_price: float = 60_000.0,
    size_usdt: float = 2000.0,
    action: str = "open",
) -> SimulatedFill:
    fee = size_usdt * 0.0005
    return SimulatedFill(
        time=_T,
        symbol="BTCUSDT",
        side=side,
        action=action,
        requested_size_usdt=size_usdt,
        fill_price=fill_price,
        fill_size_usdt=size_usdt,
        slippage_usdt=size_usdt * 0.0005,
        fee_usdt=fee,
        net_size_usdt=size_usdt - fee,
        config_hash=_HASH,
    )


# ---------------------------------------------------------------------------

class TestPositionTrackerOpenClose:
    def test_initial_no_position(self):
        assert PositionTracker().current is None

    def test_open_creates_position(self):
        tracker = PositionTracker()
        fill = _make_fill()
        pos = tracker.open(fill, _T, mark_price=60_000.0)
        assert pos is not None
        assert pos.symbol == "BTCUSDT"
        assert pos.side == PositionSide.LONG
        assert tracker.current is pos

    def test_open_twice_raises(self):
        tracker = PositionTracker()
        fill = _make_fill()
        tracker.open(fill, _T, mark_price=60_000.0)
        with pytest.raises(RuntimeError, match="already open"):
            tracker.open(fill, _T, mark_price=60_000.0)

    def test_close_removes_position(self):
        tracker = PositionTracker()
        open_fill = _make_fill()
        tracker.open(open_fill, _T, mark_price=60_000.0)
        close_fill = _make_fill(action="close")
        tracker.close(close_fill, realized_pnl=50.0)
        assert tracker.current is None

    def test_close_without_position_raises(self):
        tracker = PositionTracker()
        fill = _make_fill(action="close")
        with pytest.raises(RuntimeError, match="No open position"):
            tracker.close(fill, realized_pnl=0.0)


class TestPositionTrackerReduce:
    def test_reduce_halves_size(self):
        tracker = PositionTracker()
        open_fill = _make_fill(size_usdt=2000.0)
        tracker.open(open_fill, _T, mark_price=60_000.0)

        close_fill = _make_fill(action="close", size_usdt=1000.0)
        tracker.reduce(close_fill, realized_pnl=10.0)

        assert tracker.current is not None
        # current_size = net_size_open (2000 - fee) - 1000
        expected = open_fill.net_size_usdt - 1000.0
        assert tracker.current.current_size_usdt == pytest.approx(expected)

    def test_reduce_without_position_raises(self):
        tracker = PositionTracker()
        fill = _make_fill(action="close", size_usdt=1000.0)
        with pytest.raises(RuntimeError, match="No open position"):
            tracker.reduce(fill, realized_pnl=0.0)


class TestUnrealizedPnL:
    def test_unrealized_long_profit(self):
        tracker = PositionTracker()
        fill = _make_fill(fill_price=60_000.0, size_usdt=2000.0)
        pos = tracker.open(fill, _T, mark_price=60_000.0)

        tracker.update_unrealized(current_price=63_000.0)
        # (63000 - 60000) / 60000 * net_size
        expected = (63_000.0 - fill.fill_price) / fill.fill_price * pos.current_size_usdt
        assert pos.unrealized_pnl_usdt == pytest.approx(expected, rel=1e-6)
        assert pos.unrealized_pnl_usdt > 0

    def test_unrealized_long_loss(self):
        tracker = PositionTracker()
        fill = _make_fill(fill_price=60_000.0, size_usdt=2000.0)
        pos = tracker.open(fill, _T, mark_price=60_000.0)

        tracker.update_unrealized(current_price=57_000.0)
        assert pos.unrealized_pnl_usdt < 0

    def test_unrealized_short_profit(self):
        tracker = PositionTracker()
        fill = _make_fill(side=PositionSide.SHORT, fill_price=60_000.0, size_usdt=2000.0)
        pos = tracker.open(fill, _T, mark_price=60_000.0)

        tracker.update_unrealized(current_price=57_000.0)  # price fell
        assert pos.unrealized_pnl_usdt > 0

    def test_unrealized_short_loss(self):
        tracker = PositionTracker()
        fill = _make_fill(side=PositionSide.SHORT, fill_price=60_000.0, size_usdt=2000.0)
        pos = tracker.open(fill, _T, mark_price=60_000.0)

        tracker.update_unrealized(current_price=63_000.0)  # price rose
        assert pos.unrealized_pnl_usdt < 0

    def test_update_unrealized_no_position_noop(self):
        tracker = PositionTracker()
        tracker.update_unrealized(63_000.0)  # should not raise
        assert tracker.current is None
