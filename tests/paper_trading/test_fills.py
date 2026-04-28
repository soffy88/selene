"""Tests for FillSimulator."""
import pytest
from datetime import datetime, timezone

from decision.config import load_config
from paper_trading.fills import FillSimulator
from paper_trading.schema import Position, PositionSide


_T = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
_HASH = "abc123"


@pytest.fixture(scope="module")
def sim():
    cfg = load_config()
    return FillSimulator(cfg.execution)


@pytest.fixture(scope="module")
def exec_cfg():
    return load_config().execution


# ---------------------------------------------------------------------------

class TestSimulateOpen:
    def test_open_long_fill_price_above_mark(self, sim, exec_cfg):
        """Long open: fill_price = mark * (1 + slippage_pct) > mark."""
        mark = 60_000.0
        fill = sim.simulate_open(
            time=_T,
            symbol="BTCUSDT",
            side=PositionSide.LONG,
            size_usdt=2000.0,
            mark_price=mark,
            config_hash=_HASH,
        )
        expected_fill_price = mark * (1 + exec_cfg.slippage_open_pct)
        assert fill.fill_price == pytest.approx(expected_fill_price)
        assert fill.fill_price > mark

    def test_open_short_fill_price_below_mark(self, sim, exec_cfg):
        """Short open: fill_price = mark * (1 - slippage_pct) < mark."""
        mark = 60_000.0
        fill = sim.simulate_open(
            time=_T,
            symbol="BTCUSDT",
            side=PositionSide.SHORT,
            size_usdt=2000.0,
            mark_price=mark,
            config_hash=_HASH,
        )
        expected_fill_price = mark * (1 - exec_cfg.slippage_open_pct)
        assert fill.fill_price == pytest.approx(expected_fill_price)
        assert fill.fill_price < mark

    def test_fee_deduction(self, sim, exec_cfg):
        """Fee = taker_fee_pct × fill_size; net_size = fill_size - fee."""
        fill = sim.simulate_open(
            time=_T,
            symbol="BTCUSDT",
            side=PositionSide.LONG,
            size_usdt=2000.0,
            mark_price=60_000.0,
            config_hash=_HASH,
        )
        expected_fee = 2000.0 * exec_cfg.taker_fee_pct
        assert fill.fee_usdt == pytest.approx(expected_fee)
        assert fill.net_size_usdt == pytest.approx(fill.fill_size_usdt - fill.fee_usdt)

    def test_slippage_usdt_correct(self, sim, exec_cfg):
        size = 2000.0
        fill = sim.simulate_open(
            time=_T,
            symbol="BTCUSDT",
            side=PositionSide.LONG,
            size_usdt=size,
            mark_price=60_000.0,
            config_hash=_HASH,
        )
        assert fill.slippage_usdt == pytest.approx(size * exec_cfg.slippage_open_pct)

    def test_action_field_is_open(self, sim):
        fill = sim.simulate_open(
            time=_T,
            symbol="BTCUSDT",
            side=PositionSide.LONG,
            size_usdt=1000.0,
            mark_price=50_000.0,
            config_hash=_HASH,
        )
        assert fill.action == "open"


class TestSimulateClose:
    def _open_position(self, entry_price: float, size: float, side: PositionSide) -> Position:
        return Position(
            symbol="BTCUSDT",
            side=side,
            open_time=_T,
            open_price=entry_price,
            size_usdt=size,
            current_size_usdt=size,
        )

    def test_close_long_with_profit(self, sim, exec_cfg):
        """Close long at price above entry → positive realized_pnl."""
        entry = 60_000.0
        mark = 63_000.0  # +5%
        size = 2000.0
        pos = self._open_position(entry, size, PositionSide.LONG)

        fill, pnl = sim.simulate_close(
            time=_T,
            symbol="BTCUSDT",
            position=pos,
            mark_price=mark,
            config_hash=_HASH,
        )
        assert pnl > 0
        assert fill.action == "close"
        # fill_price for long close: mark * (1 - slippage)
        expected_fill_price = mark * (1 - exec_cfg.slippage_close_pct)
        assert fill.fill_price == pytest.approx(expected_fill_price)

    def test_close_long_with_loss(self, sim):
        """Close long at price below entry → negative realized_pnl."""
        entry = 60_000.0
        mark = 57_000.0  # -5%
        pos = self._open_position(entry, 2000.0, PositionSide.LONG)
        _, pnl = sim.simulate_close(
            time=_T, symbol="BTCUSDT", position=pos, mark_price=mark, config_hash=_HASH
        )
        assert pnl < 0

    def test_close_short_with_profit(self, sim):
        """Close short at price below entry → positive realized_pnl."""
        entry = 60_000.0
        mark = 57_000.0  # price fell
        pos = self._open_position(entry, 2000.0, PositionSide.SHORT)
        _, pnl = sim.simulate_close(
            time=_T, symbol="BTCUSDT", position=pos, mark_price=mark, config_hash=_HASH
        )
        assert pnl > 0

    def test_partial_close_uses_specified_size(self, sim):
        """Partial close: fill's requested_size equals size_to_close_usdt."""
        pos = self._open_position(60_000.0, 2000.0, PositionSide.LONG)
        fill, _ = sim.simulate_close(
            time=_T,
            symbol="BTCUSDT",
            position=pos,
            mark_price=60_000.0,
            config_hash=_HASH,
            size_to_close_usdt=1000.0,
        )
        assert fill.requested_size_usdt == pytest.approx(1000.0)
        assert fill.fill_size_usdt == pytest.approx(1000.0)

    def test_realized_pnl_formula_long(self, sim, exec_cfg):
        """Verify exact PnL formula for long."""
        entry = 60_000.0
        mark = 63_000.0
        size = 2000.0
        pos = self._open_position(entry, size, PositionSide.LONG)
        fill, pnl = sim.simulate_close(
            time=_T, symbol="BTCUSDT", position=pos, mark_price=mark, config_hash=_HASH
        )
        fill_price = mark * (1 - exec_cfg.slippage_close_pct)
        fee = size * exec_cfg.taker_fee_pct
        expected_pnl = (fill_price - entry) / entry * size - fee
        assert pnl == pytest.approx(expected_pnl, rel=1e-6)
