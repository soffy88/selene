"""Tests for DecisionEngine."""
import pytest
from datetime import datetime, timezone

from decision.config import load_config
from paper_trading.engine import DecisionEngine
from paper_trading.schema import (
    AccountState,
    DecisionAction,
    Position,
    PositionSide,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def cfg():
    return load_config()


@pytest.fixture(scope="module")
def engine(cfg):
    return DecisionEngine(cfg)


def _account(cfg, nav: float = 10_000.0) -> AccountState:
    return AccountState(
        time=datetime(2026, 4, 28, tzinfo=timezone.utc),
        nav_usdt=nav,
        realized_pnl_usdt=0.0,
        unrealized_pnl_usdt=0.0,
        position_side=None,
        position_size_usdt=0.0,
        daily_drawdown_pct=0.0,
        consecutive_losses=0,
        is_paused=False,
        pause_until=None,
        cascade_cooling=False,
    )


def _position(side: PositionSide = PositionSide.LONG, size: float = 2000.0) -> Position:
    return Position(
        symbol="BTCUSDT",
        side=side,
        open_time=datetime(2026, 4, 28, tzinfo=timezone.utc),
        open_price=60_000.0,
        size_usdt=size,
        current_size_usdt=size,
    )


_T = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class TestDecisionEngineColdStart:
    def test_cold_start_returns_no_action(self, engine, cfg):
        account = _account(cfg)
        decision = engine.decide(
            current_state=None,
            previous_state=None,
            current_position=None,
            account=account,
            bar_time=_T,
            current_price=60_000.0,
        )
        assert decision.action == DecisionAction.NO_ACTION
        assert decision.rule_id == "cold_start"
        assert decision.target_size_usdt is None

    def test_none_reason_encoded_in_rule_id(self, engine, cfg):
        """none_reason parameter is encoded as 'none:<reason>' in rule_id."""
        account = _account(cfg)
        for reason in ("cold_start", "missing_data", "no_match"):
            decision = engine.decide(
                current_state=None,
                previous_state=None,
                current_position=None,
                account=account,
                bar_time=_T,
                current_price=60_000.0,
                none_reason=reason,
            )
            assert decision.action == DecisionAction.NO_ACTION
            assert decision.rule_id == f"none:{reason}"

    def test_none_reason_none_falls_back_to_cold_start(self, engine, cfg):
        """When none_reason is not passed, rule_id stays 'cold_start' for backward compat."""
        account = _account(cfg)
        decision = engine.decide(
            current_state=None,
            previous_state=None,
            current_position=None,
            account=account,
            bar_time=_T,
            current_price=60_000.0,
            none_reason=None,
        )
        assert decision.rule_id == "cold_start"


class TestDecisionEngineCascade:
    def test_cascade_state_returns_close(self, engine, cfg):
        account = _account(cfg)
        pos = _position()
        decision = engine.decide(
            current_state="Cascade",
            previous_state="Surging_Up",
            current_position=pos,
            account=account,
            bar_time=_T,
            current_price=60_000.0,
        )
        assert decision.action == DecisionAction.CLOSE
        assert decision.rule_id == "cascade_force_close"
        assert decision.target_size_usdt == 0.0

    def test_cascade_no_position_still_close(self, engine, cfg):
        account = _account(cfg)
        decision = engine.decide(
            current_state="Cascade",
            previous_state="Drifting_Calm",
            current_position=None,
            account=account,
            bar_time=_T,
            current_price=60_000.0,
        )
        assert decision.action == DecisionAction.CLOSE
        assert decision.target_size_usdt == 0.0


class TestDecisionEngineSurgingUp:
    def test_surging_up_from_coiling_open_long(self, engine, cfg):
        account = _account(cfg)
        decision = engine.decide(
            current_state="Surging_Up",
            previous_state="Coiling",
            current_position=None,
            account=account,
            bar_time=_T,
            current_price=60_000.0,
        )
        assert decision.action == DecisionAction.OPEN_LONG
        assert decision.rule_id == "surging_up_from_coiling"
        # target = 10000 * 0.20 * 1.0 = 2000
        assert decision.target_size_usdt == pytest.approx(2000.0)

    def test_surging_up_from_drifting_calm_no_action(self, engine, cfg):
        """Surging_Up NOT from Coiling → no_action (rule surging_up_not_from_coiling)."""
        account = _account(cfg)
        decision = engine.decide(
            current_state="Surging_Up",
            previous_state="Drifting_Calm",
            current_position=None,
            account=account,
            bar_time=_T,
            current_price=60_000.0,
        )
        assert decision.action == DecisionAction.NO_ACTION
        assert decision.target_size_usdt == 0.0

    def test_surging_up_from_surging_down_close(self, engine, cfg):
        """Direction reversal — should CLOSE the short position."""
        account = _account(cfg)
        pos = _position(side=PositionSide.SHORT)
        decision = engine.decide(
            current_state="Surging_Up",
            previous_state="Surging_Down",
            current_position=pos,
            account=account,
            bar_time=_T,
            current_price=60_000.0,
        )
        assert decision.action == DecisionAction.CLOSE
        assert decision.rule_id == "reverse_surging_close_short"
        assert decision.target_size_usdt == 0.0


class TestDecisionEngineDriftingCharged:
    def test_drifting_charged_from_surging_hold_half(self, engine, cfg):
        """Drifting_Charged after Surging_Up → HOLD with 0.5× target."""
        account = _account(cfg)
        pos = _position(size=2000.0)
        decision = engine.decide(
            current_state="Drifting_Charged",
            previous_state="Surging_Up",
            current_position=pos,
            account=account,
            bar_time=_T,
            current_price=60_000.0,
        )
        assert decision.action == DecisionAction.HOLD
        assert decision.rule_id == "drifting_charged_after_surging_reduce"
        # target = 2000 * 0.5 = 1000
        assert decision.target_size_usdt == pytest.approx(1000.0)
        assert decision.position_multiplier == pytest.approx(0.5)
