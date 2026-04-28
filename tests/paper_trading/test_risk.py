"""Tests for RiskGate — Wave 3 risk layer.

Each test targets a single risk rule.  All fixtures use synthetic objects so
no database or network connection is required.
"""
from __future__ import annotations

import pytest
from datetime import datetime, timedelta, timezone

from decision.config import load_config, RiskConfig
from paper_trading.risk import RiskGate, RiskCheckResult
from paper_trading.schema import (
    AccountState,
    DecisionAction,
    Position,
    PositionSide,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_T = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
_SYMBOL = "BTCUSDT"


@pytest.fixture(scope="module")
def risk_cfg() -> RiskConfig:
    return load_config().risk


def _gate(cfg: RiskConfig) -> RiskGate:
    return RiskGate(cfg, _SYMBOL)


def _account(
    nav: float = 10_000.0,
    daily_drawdown_pct: float = 0.0,
    consecutive_losses: int = 0,
    is_paused: bool = False,
    pause_until: datetime | None = None,
) -> AccountState:
    return AccountState(
        time=_T,
        nav_usdt=nav,
        realized_pnl_usdt=0.0,
        unrealized_pnl_usdt=0.0,
        position_side=None,
        position_size_usdt=0.0,
        daily_drawdown_pct=daily_drawdown_pct,
        consecutive_losses=consecutive_losses,
        is_paused=is_paused,
        pause_until=pause_until,
        cascade_cooling=False,
    )


def _position(unrealized: float = 0.0, size: float = 2_000.0) -> Position:
    return Position(
        symbol=_SYMBOL,
        side=PositionSide.LONG,
        open_time=_T,
        open_price=60_000.0,
        size_usdt=size,
        current_size_usdt=size,
        unrealized_pnl_usdt=unrealized,
    )


def _check(
    gate: RiskGate,
    proposed: DecisionAction = DecisionAction.OPEN_LONG,
    state: str | None = "Surging_Up",
    bar_time: datetime = _T,
    position: Position | None = None,
    account: AccountState | None = None,
    last_update: datetime | None = None,
) -> RiskCheckResult:
    if account is None:
        account = _account()
    return gate.check(
        proposed_action=proposed,
        current_state=state,
        bar_time=bar_time,
        position=position,
        account=account,
        last_state_update_time=last_update,
    )


# ---------------------------------------------------------------------------
# Test 1: CASCADE always fires regardless of other state
# ---------------------------------------------------------------------------

class TestCascadeRule:
    def test_cascade_forces_close(self, risk_cfg):
        gate = _gate(risk_cfg)
        result = _check(gate, state="Cascade")
        assert result.passed is False
        assert result.force_action == DecisionAction.CLOSE
        assert result.triggered_rule == "cascade"

    def test_cascade_with_hold_action_still_closes(self, risk_cfg):
        gate = _gate(risk_cfg)
        result = _check(gate, proposed=DecisionAction.HOLD, state="Cascade")
        assert result.passed is False
        assert result.force_action == DecisionAction.CLOSE

    def test_non_cascade_state_does_not_fire(self, risk_cfg):
        gate = _gate(risk_cfg)
        result = _check(gate, state="Surging_Up")
        # Should not be blocked by cascade rule
        assert result.triggered_rule != "cascade"


# ---------------------------------------------------------------------------
# Test 2: Signal lag
# ---------------------------------------------------------------------------

class TestSignalLagRule:
    def test_lag_beyond_threshold_fires(self, risk_cfg):
        gate = _gate(risk_cfg)
        stale_time = _T - timedelta(hours=risk_cfg.signal_lag_max_hours + 1)
        result = _check(gate, last_update=stale_time)
        assert result.passed is False
        assert result.force_action == DecisionAction.CLOSE
        assert result.triggered_rule == "signal_lag"

    def test_lag_within_threshold_passes(self, risk_cfg):
        gate = _gate(risk_cfg)
        recent = _T - timedelta(hours=risk_cfg.signal_lag_max_hours - 0.5)
        result = _check(gate, last_update=recent)
        assert result.triggered_rule != "signal_lag"

    def test_no_last_update_passes_lag_rule(self, risk_cfg):
        """None last_update means no lag check — should not fire signal_lag."""
        gate = _gate(risk_cfg)
        result = _check(gate, last_update=None)
        assert result.triggered_rule != "signal_lag"


# ---------------------------------------------------------------------------
# Test 3: Single trade max loss
# ---------------------------------------------------------------------------

class TestSingleTradeMaxLoss:
    def test_large_loss_fires(self, risk_cfg):
        """Unrealized loss > max_loss_per_trade_pct × NAV → CLOSE."""
        nav = 10_000.0
        # loss_pct = 400/10000 = 4% ; threshold typically 3%
        pos = _position(unrealized=-nav * (risk_cfg.max_loss_per_trade_pct + 0.01))
        gate = _gate(risk_cfg)
        result = _check(gate, position=pos, account=_account(nav=nav))
        assert result.passed is False
        assert result.force_action == DecisionAction.CLOSE
        assert result.triggered_rule == "single_trade_max_loss"

    def test_small_loss_passes(self, risk_cfg):
        nav = 10_000.0
        # loss_pct = 1% ; threshold typically 3%
        pos = _position(unrealized=-nav * (risk_cfg.max_loss_per_trade_pct - 0.01))
        gate = _gate(risk_cfg)
        result = _check(gate, position=pos, account=_account(nav=nav))
        assert result.triggered_rule != "single_trade_max_loss"

    def test_no_position_skips_rule(self, risk_cfg):
        gate = _gate(risk_cfg)
        result = _check(gate, position=None)
        assert result.triggered_rule != "single_trade_max_loss"


# ---------------------------------------------------------------------------
# Test 4: Daily drawdown
# ---------------------------------------------------------------------------

class TestDailyDrawdownRule:
    def test_exceeded_fires_no_action(self, risk_cfg):
        gate = _gate(risk_cfg)
        acc = _account(
            daily_drawdown_pct=risk_cfg.max_daily_drawdown_pct + 0.01
        )
        result = _check(gate, account=acc)
        assert result.passed is False
        assert result.force_action == DecisionAction.NO_ACTION
        assert result.triggered_rule == "daily_drawdown"

    def test_below_threshold_passes(self, risk_cfg):
        gate = _gate(risk_cfg)
        acc = _account(daily_drawdown_pct=risk_cfg.max_daily_drawdown_pct - 0.01)
        result = _check(gate, account=acc)
        assert result.triggered_rule != "daily_drawdown"


# ---------------------------------------------------------------------------
# Test 5: Consecutive loss pause (already in pause window)
# ---------------------------------------------------------------------------

class TestConsecutiveLossPause:
    def test_open_blocked_during_pause(self, risk_cfg):
        gate = _gate(risk_cfg)
        gate._pause_until = _T + timedelta(hours=12)  # mid-pause
        result = _check(gate, proposed=DecisionAction.OPEN_LONG)
        assert result.passed is False
        assert result.force_action == DecisionAction.NO_ACTION
        assert result.triggered_rule == "consecutive_loss_pause"

    def test_close_allowed_during_pause(self, risk_cfg):
        """CLOSE should pass through even when in pause (closes are always ok)."""
        gate = _gate(risk_cfg)
        gate._pause_until = _T + timedelta(hours=12)
        result = _check(gate, proposed=DecisionAction.CLOSE)
        assert result.triggered_rule != "consecutive_loss_pause"

    def test_pause_expired_no_block(self, risk_cfg):
        gate = _gate(risk_cfg)
        gate._pause_until = _T - timedelta(hours=1)  # already expired
        result = _check(gate, proposed=DecisionAction.OPEN_LONG)
        assert result.triggered_rule != "consecutive_loss_pause"


# ---------------------------------------------------------------------------
# Test 6: Consecutive loss stop triggers new pause
# ---------------------------------------------------------------------------

class TestConsecutiveLossStop:
    def test_stop_sets_pause_until(self, risk_cfg):
        gate = _gate(risk_cfg)
        acc = _account(consecutive_losses=risk_cfg.consecutive_loss_stop)
        result = _check(gate, account=acc, bar_time=_T)
        assert result.passed is False
        assert result.force_action == DecisionAction.NO_ACTION
        assert result.triggered_rule == "consecutive_loss_stop"
        assert gate._pause_until is not None
        # Pause should be ~24H from bar_time
        expected = _T + timedelta(hours=24)
        delta = abs((gate._pause_until - expected).total_seconds())
        assert delta < 1  # within 1 second

    def test_below_stop_does_not_pause(self, risk_cfg):
        gate = _gate(risk_cfg)
        acc = _account(consecutive_losses=risk_cfg.consecutive_loss_stop - 1)
        result = _check(gate, account=acc)
        assert result.triggered_rule != "consecutive_loss_stop"
        assert gate._pause_until is None


# ---------------------------------------------------------------------------
# Test 7: All rules pass → RiskCheckResult.passed is True
# ---------------------------------------------------------------------------

class TestAllRulesPass:
    def test_clean_account_passes(self, risk_cfg):
        gate = _gate(risk_cfg)
        result = _check(
            gate,
            proposed=DecisionAction.OPEN_LONG,
            state="Surging_Up",
            bar_time=_T,
            position=_position(unrealized=50.0),  # small gain
            account=_account(
                nav=10_000.0,
                daily_drawdown_pct=0.0,
                consecutive_losses=0,
            ),
            last_update=_T - timedelta(minutes=30),
        )
        assert result.passed is True
        assert result.force_action is None
        assert result.triggered_rule is None
        assert result.details == "ok"


# ---------------------------------------------------------------------------
# Test 8: CASCADE fires even during 24H pause (priority override)
# ---------------------------------------------------------------------------

class TestCascadeOverridesPause:
    def test_cascade_during_pause_still_closes(self, risk_cfg):
        gate = _gate(risk_cfg)
        gate._pause_until = _T + timedelta(hours=20)  # active pause
        result = _check(
            gate,
            proposed=DecisionAction.OPEN_LONG,
            state="Cascade",
        )
        assert result.passed is False
        assert result.force_action == DecisionAction.CLOSE
        assert result.triggered_rule == "cascade"


# ---------------------------------------------------------------------------
# Test 9: to_state_dict / from_state_dict round-trip
# ---------------------------------------------------------------------------

class TestStatePersistence:
    def test_roundtrip_with_pause(self, risk_cfg):
        gate = _gate(risk_cfg)
        gate._pause_until = _T + timedelta(hours=18)
        gate._consecutive_losses = 3

        state = gate.to_state_dict()
        assert state["pause_until"] is not None
        assert state["consecutive_losses"] == 3

        restored = RiskGate.from_state_dict(risk_cfg, _SYMBOL, state)
        assert restored._pause_until == gate._pause_until
        assert restored._consecutive_losses == 3

    def test_roundtrip_no_pause(self, risk_cfg):
        gate = _gate(risk_cfg)
        state = gate.to_state_dict()
        assert state["pause_until"] is None

        restored = RiskGate.from_state_dict(risk_cfg, _SYMBOL, state)
        assert restored._pause_until is None
        assert restored._consecutive_losses == 0

    def test_from_empty_dict(self, risk_cfg):
        restored = RiskGate.from_state_dict(risk_cfg, _SYMBOL, {})
        assert restored._pause_until is None
        assert restored._consecutive_losses == 0


# ---------------------------------------------------------------------------
# Test 10: recent_events returns correct count
# ---------------------------------------------------------------------------

class TestRecentEvents:
    def test_returns_at_most_n_events(self, risk_cfg):
        gate = _gate(risk_cfg)
        # Fire cascade 5 times (simulate multiple bars)
        for _ in range(5):
            _check(gate, state="Cascade")

        assert len(gate.recent_events(n=3)) == 3
        assert len(gate.recent_events(n=10)) == 5  # only 5 events total

    def test_empty_gate_returns_empty(self, risk_cfg):
        gate = _gate(risk_cfg)
        assert gate.recent_events() == []

    def test_events_ordered_oldest_first(self, risk_cfg):
        """Events appended in call order — oldest event is index 0."""
        gate = _gate(risk_cfg)
        t1 = _T
        t2 = _T + timedelta(hours=1)
        # Two lag-rule fires at different bar_times
        stale = _T - timedelta(hours=risk_cfg.signal_lag_max_hours + 5)
        _check(gate, bar_time=t1, last_update=stale)
        _check(gate, bar_time=t2, last_update=stale)

        events = gate.recent_events(n=20)
        assert events[0].time == t1
        assert events[1].time == t2


# ---------------------------------------------------------------------------
# Test 11: Rule 2 subtype (missing_data vs no_match)
# ---------------------------------------------------------------------------

class TestSignalLagRule2Subtype:
    """Rule 2 tags the correct subtype based on none_reason distribution in lag window."""

    def _stale(self, risk_cfg: RiskConfig) -> datetime:
        return _T - timedelta(hours=risk_cfg.signal_lag_max_hours + 1)

    def test_rule_2_subtype_missing_data_when_any_missing(self, risk_cfg):
        gate = _gate(risk_cfg)
        none_reasons = {"missing_data": 18, "no_match": 6, "cold_start": 0}
        result = gate.check(
            proposed_action=DecisionAction.OPEN_LONG,
            current_state="Surging_Up",
            bar_time=_T,
            position=None,
            account=_account(),
            last_state_update_time=self._stale(risk_cfg),
            none_reasons_in_lag=none_reasons,
        )
        assert result.triggered_rule == "signal_lag"
        assert result.rule_2_subtype == "missing_data"
        assert result.none_reasons_in_lag == none_reasons

    def test_rule_2_subtype_no_match_when_no_missing_data(self, risk_cfg):
        gate = _gate(risk_cfg)
        none_reasons = {"missing_data": 0, "no_match": 24, "cold_start": 0}
        result = gate.check(
            proposed_action=DecisionAction.OPEN_LONG,
            current_state="Surging_Up",
            bar_time=_T,
            position=None,
            account=_account(),
            last_state_update_time=self._stale(risk_cfg),
            none_reasons_in_lag=none_reasons,
        )
        assert result.triggered_rule == "signal_lag"
        assert result.rule_2_subtype == "no_match"

    def test_rule_2_subtype_defaults_to_no_match_when_reasons_none(self, risk_cfg):
        """Backward compat: when none_reasons_in_lag not passed, subtype is 'no_match'."""
        gate = _gate(risk_cfg)
        result = gate.check(
            proposed_action=DecisionAction.OPEN_LONG,
            current_state="Surging_Up",
            bar_time=_T,
            position=None,
            account=_account(),
            last_state_update_time=self._stale(risk_cfg),
        )
        assert result.triggered_rule == "signal_lag"
        assert result.rule_2_subtype == "no_match"
        assert result.force_action == DecisionAction.CLOSE  # candidate A: always CLOSE

    def test_rule_2_candidate_b_different_action_per_subtype(self, risk_cfg):
        """Candidate B: missing_data → NO_ACTION (HOLD + alert); no_match → CLOSE."""
        gate = _gate(risk_cfg)
        stale = self._stale(risk_cfg)

        missing_result = gate.check(
            proposed_action=DecisionAction.OPEN_LONG,
            current_state="Surging_Up",
            bar_time=_T,
            position=None,
            account=_account(),
            last_state_update_time=stale,
            none_reasons_in_lag={"missing_data": 10, "no_match": 0, "cold_start": 0},
        )
        assert missing_result.force_action == DecisionAction.NO_ACTION
        assert missing_result.alert_required is True

        no_match_result = gate.check(
            proposed_action=DecisionAction.OPEN_LONG,
            current_state="Surging_Up",
            bar_time=_T,
            position=None,
            account=_account(),
            last_state_update_time=stale,
            none_reasons_in_lag={"missing_data": 0, "no_match": 10, "cold_start": 0},
        )
        assert no_match_result.force_action == DecisionAction.CLOSE
        assert no_match_result.alert_required is False


# ---------------------------------------------------------------------------
# Test 12: Candidate B — full behaviour verification
# ---------------------------------------------------------------------------

class TestCandidateB:
    """Candidate B: missing_data → HOLD + alert; no_match → CLOSE. Rule 1 priority."""

    def _stale(self, cfg: RiskConfig) -> datetime:
        return _T - timedelta(hours=cfg.signal_lag_max_hours + 1)

    def test_rule_2_missing_data_holds_not_closes(self, risk_cfg):
        gate = _gate(risk_cfg)
        result = gate.check(
            proposed_action=DecisionAction.OPEN_LONG,
            current_state=None,
            bar_time=_T,
            position=_position(),
            account=_account(),
            last_state_update_time=self._stale(risk_cfg),
            none_reasons_in_lag={"missing_data": 18, "no_match": 6, "cold_start": 0},
        )
        assert result.force_action == DecisionAction.NO_ACTION
        assert result.alert_required is True
        assert result.rule_2_subtype == "missing_data"
        assert result.triggered_rule == "signal_lag"

    def test_rule_2_no_match_closes(self, risk_cfg):
        gate = _gate(risk_cfg)
        result = gate.check(
            proposed_action=DecisionAction.OPEN_LONG,
            current_state=None,
            bar_time=_T,
            position=None,
            account=_account(),
            last_state_update_time=self._stale(risk_cfg),
            none_reasons_in_lag={"missing_data": 0, "no_match": 24, "cold_start": 0},
        )
        assert result.force_action == DecisionAction.CLOSE
        assert result.alert_required is False
        assert result.rule_2_subtype == "no_match"

    def test_rule_2_mixed_18_missing_6_no_match_holds(self, risk_cfg):
        """Any missing_data in window → treated as missing_data subtype → HOLD."""
        gate = _gate(risk_cfg)
        result = gate.check(
            proposed_action=DecisionAction.OPEN_LONG,
            current_state=None,
            bar_time=_T,
            position=None,
            account=_account(),
            last_state_update_time=self._stale(risk_cfg),
            none_reasons_in_lag={"missing_data": 18, "no_match": 6, "cold_start": 0},
        )
        assert result.force_action == DecisionAction.NO_ACTION
        assert result.rule_2_subtype == "missing_data"

    def test_rule_1_cascade_overrides_rule_2_missing_data_hold(self, risk_cfg):
        """Rule 1 (Cascade CLOSE) must fire before Rule 2 even with missing_data in lag."""
        gate = _gate(risk_cfg)
        stale = self._stale(risk_cfg)
        result = gate.check(
            proposed_action=DecisionAction.OPEN_LONG,
            current_state="Cascade",
            bar_time=_T,
            position=_position(),
            account=_account(),
            last_state_update_time=stale,
            none_reasons_in_lag={"missing_data": 20, "no_match": 0, "cold_start": 0},
        )
        assert result.force_action == DecisionAction.CLOSE
        assert result.triggered_rule == "cascade"
