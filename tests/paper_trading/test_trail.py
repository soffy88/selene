"""Tests for Wave 4: DecisionTrailBuilder, DecisionTrail, DecisionReplayer."""
from __future__ import annotations

import pytest
from datetime import datetime, timedelta, timezone
from typing import Optional

from decision.config import load_config, DecisionConfig, DecisionRule
from paper_trading.schema import (
    AccountState,
    Decision,
    DecisionAction,
    Position,
    PositionSide,
    SimulatedFill,
)
from paper_trading.risk import RiskCheckResult
from paper_trading.trail import DecisionTrail, DecisionTrailBuilder
from paper_trading.replay import DecisionReplayer


# ---------------------------------------------------------------------------
# Shared helpers / fixtures
# ---------------------------------------------------------------------------

_T = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
_SYMBOL = "BTCUSDT"


@pytest.fixture(scope="module")
def cfg() -> DecisionConfig:
    return load_config()


def _account(
    nav: float = 10_000.0,
    realized_pnl: float = 0.0,
    unrealized_pnl: float = 0.0,
    position_side: Optional[str] = None,
    position_size: float = 0.0,
) -> AccountState:
    return AccountState(
        time=_T,
        nav_usdt=nav,
        realized_pnl_usdt=realized_pnl,
        unrealized_pnl_usdt=unrealized_pnl,
        position_side=position_side,
        position_size_usdt=position_size,
        daily_drawdown_pct=0.0,
        consecutive_losses=0,
        is_paused=False,
        pause_until=None,
        cascade_cooling=False,
    )


def _state_output(
    state: Optional[str] = "Surging_Up",
    prev_state: Optional[str] = "Coiling",
    close: float = 60_000.0,
    feature_completeness: float = 0.44,
    none_reason: Optional[str] = None,
):
    """Create a minimal duck-typed StateOutput."""
    class _SO:
        pass
    so = _SO()
    so.bar_time = _T
    so.symbol = _SYMBOL
    so.state = state
    so.state_enum = None
    so.direction = None
    so.is_cold_start = state is None
    so.confidence = 1.0
    so.reason = f"test:{state}"
    so.is_legal_transition = True
    so.transition_from = prev_state
    so.health_warning = False
    so.close = close
    so.sigma_p_24h = 0.15
    so.H = 0.45
    so.TF = None
    so.OI = None
    so.funding_rate = 0.0001
    so.LV = None
    so.absorption_ratio = None
    so.OI_hurst = None
    so.feature_completeness = feature_completeness
    so.none_reason = none_reason
    return so


def _decision(
    action: DecisionAction = DecisionAction.OPEN_LONG,
    rule_id: str = "surging_up_from_coiling",
    target_size_usdt: Optional[float] = 2_000.0,
    cfg: Optional[DecisionConfig] = None,
) -> Decision:
    config_hash = cfg.config_hash if cfg else "test0000deadbeef"
    return Decision(
        time=_T,
        symbol=_SYMBOL,
        action=action,
        rule_id=rule_id,
        config_hash=config_hash,
        current_state="Surging_Up",
        previous_state="Coiling",
        position_multiplier=1.0,
        target_size_usdt=target_size_usdt,
        reason="test decision",
    )


def _risk_passed() -> RiskCheckResult:
    return RiskCheckResult(passed=True, force_action=None, triggered_rule=None, details="ok")


def _risk_triggered(rule: str = "single_trade_max_loss") -> RiskCheckResult:
    return RiskCheckResult(
        passed=False,
        force_action=DecisionAction.CLOSE,
        triggered_rule=rule,
        details=f"risk rule {rule} fired",
    )


def _fill(price: float = 60_100.0, size: float = 2_000.0, fee: float = 1.0) -> SimulatedFill:
    return SimulatedFill(
        time=_T,
        symbol=_SYMBOL,
        side=PositionSide.LONG,
        action="open",
        requested_size_usdt=size,
        fill_price=price,
        fill_size_usdt=size,
        slippage_usdt=30.0,
        fee_usdt=fee,
        net_size_usdt=size - fee,
        config_hash="test0000deadbeef",
    )


# ---------------------------------------------------------------------------
# Test 1: build() assembles all fields correctly
# ---------------------------------------------------------------------------

class TestDecisionTrailBuild:
    def test_build_assembles_all_fields(self, cfg):
        builder = DecisionTrailBuilder(_SYMBOL, cfg)
        builder.record_state("Surging_Up")
        so = _state_output()
        dec = _decision(cfg=cfg)
        risk = _risk_passed()
        account = _account()

        trail = builder.build(_T, so, dec, risk, None, None, account)

        assert trail.time == _T
        assert trail.symbol == _SYMBOL
        assert trail.config_hash == cfg.config_hash
        assert trail.current_state == "Surging_Up"
        assert trail.previous_state == "Coiling"
        assert trail.state_reason == "test:Surging_Up"
        assert trail.close == 60_000.0
        assert trail.sigma_p_24h == pytest.approx(0.15)
        assert trail.H == pytest.approx(0.45)
        assert trail.nav_usdt == pytest.approx(10_000.0)
        assert trail.proposed_action == "open_long"
        assert trail.final_action == "open_long"
        assert trail.rule_id == "surging_up_from_coiling"
        assert trail.risk_check_passed is True
        assert trail.risk_triggered is None
        assert trail.risk_details == "ok"
        # No fill
        assert trail.fill_price is None
        assert trail.fill_size_usdt is None
        assert trail.fill_fee_usdt is None
        assert trail.realized_pnl_this_bar is None


# ---------------------------------------------------------------------------
# Test 2: state_history maintained across multiple bars (deque maxlen 12)
# ---------------------------------------------------------------------------

class TestStateHistoryDeque:
    def test_state_history_grows_per_bar(self, cfg):
        builder = DecisionTrailBuilder(_SYMBOL, cfg)
        states = ["Coiling", "Surging_Up", "Drifting_Charged"]
        for s in states:
            builder.record_state(s)

        so = _state_output(state="Drifting_Charged", prev_state="Surging_Up")
        trail = builder.build(_T, so, _decision(cfg=cfg), _risk_passed(), None, None, _account())
        # state_history contains all 3 recorded states
        assert trail.state_history == ["Coiling", "Surging_Up", "Drifting_Charged"]

    def test_state_history_capped_at_12(self, cfg):
        builder = DecisionTrailBuilder(_SYMBOL, cfg)
        for i in range(20):
            builder.record_state(f"State{i}")

        so = _state_output()
        trail = builder.build(_T, so, _decision(cfg=cfg), _risk_passed(), None, None, _account())
        # maxlen=12 enforced by deque
        assert len(trail.state_history) == 12
        # Should be the last 12 states recorded
        assert trail.state_history[0] == "State8"
        assert trail.state_history[-1] == "State19"

    def test_none_state_recorded_as_string(self, cfg):
        builder = DecisionTrailBuilder(_SYMBOL, cfg)
        builder.record_state(None)

        so = _state_output(state=None, prev_state=None)
        dec = _decision(action=DecisionAction.NO_ACTION, rule_id="cold_start", cfg=cfg)
        trail = builder.build(_T, so, dec, _risk_passed(), None, None, _account())
        assert "None" in trail.state_history


# ---------------------------------------------------------------------------
# Test 3: using_claude_default is True per config metadata
# ---------------------------------------------------------------------------

class TestUsingClaudeDefault:
    def test_using_claude_default_is_true(self, cfg):
        builder = DecisionTrailBuilder(_SYMBOL, cfg)
        builder.record_state("Coiling")
        so = _state_output()
        trail = builder.build(_T, so, _decision(cfg=cfg), _risk_passed(), None, None, _account())
        assert trail.using_claude_default is True


# ---------------------------------------------------------------------------
# Test 4: feature_completeness is correctly computed
# ---------------------------------------------------------------------------

class TestFeatureCompleteness:
    def test_completeness_reflects_non_none_features(self, cfg):
        builder = DecisionTrailBuilder(_SYMBOL, cfg)
        builder.record_state("Coiling")
        # State output has close, sigma_p_24h, H, funding_rate non-None (4 out of 9)
        so = _state_output()
        trail = builder.build(_T, so, _decision(cfg=cfg), _risk_passed(), None, None, _account())
        # 4 non-None out of 9 features (close, sigma_p_24h, H, funding_rate + 0 of TF/OI/LV/absorption/OI_hurst)
        # close=60000, sigma_p_24h=0.15, H=0.45, funding_rate=0.0001 → 4 non-None
        # TF=None, OI=None, LV=None, absorption_ratio=None, OI_hurst=None
        assert trail.feature_completeness == pytest.approx(4 / 9)

    def test_completeness_zero_when_all_none(self, cfg):
        builder = DecisionTrailBuilder(_SYMBOL, cfg)
        builder.record_state(None)

        class _EmptySO:
            bar_time = _T
            symbol = _SYMBOL
            state = None
            state_enum = None
            direction = None
            is_cold_start = True
            confidence = 1.0
            reason = "cold"
            is_legal_transition = True
            transition_from = None
            health_warning = False
            close = None
            sigma_p_24h = None
            H = None
            TF = None
            OI = None
            funding_rate = None
            LV = None
            absorption_ratio = None
            OI_hurst = None
            feature_completeness = 0.0
            none_reason = "cold_start"

        dec = _decision(action=DecisionAction.NO_ACTION, rule_id="cold_start", cfg=cfg)
        trail = builder.build(_T, _EmptySO(), dec, _risk_passed(), None, None, _account())
        assert trail.feature_completeness == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Test 5: Fill fields are None when no fill
# ---------------------------------------------------------------------------

class TestFillFieldsNoFill:
    def test_fill_fields_none_when_no_fill(self, cfg):
        builder = DecisionTrailBuilder(_SYMBOL, cfg)
        builder.record_state("Coiling")
        so = _state_output()
        trail = builder.build(_T, so, _decision(cfg=cfg), _risk_passed(), None, None, _account())
        assert trail.fill_price is None
        assert trail.fill_size_usdt is None
        assert trail.fill_fee_usdt is None
        assert trail.realized_pnl_this_bar is None


# ---------------------------------------------------------------------------
# Test 6: Fill fields populated when fill provided
# ---------------------------------------------------------------------------

class TestFillFieldsWithFill:
    def test_fill_fields_populated(self, cfg):
        builder = DecisionTrailBuilder(_SYMBOL, cfg)
        builder.record_state("Surging_Up")
        so = _state_output()
        f = _fill(price=60_150.0, size=2_000.0, fee=1.0)
        trail = builder.build(_T, so, _decision(cfg=cfg), _risk_passed(), f, None, _account())
        assert trail.fill_price == pytest.approx(60_150.0)
        assert trail.fill_size_usdt == pytest.approx(2_000.0)
        assert trail.fill_fee_usdt == pytest.approx(1.0)
        assert trail.realized_pnl_this_bar is None  # open fill has no realized pnl

    def test_fill_with_realized_pnl(self, cfg):
        builder = DecisionTrailBuilder(_SYMBOL, cfg)
        builder.record_state("Drifting_Calm")
        so = _state_output(state="Drifting_Calm", prev_state="Surging_Up")
        f = _fill(price=60_200.0, size=2_000.0, fee=1.0)
        realized = 85.5
        trail = builder.build(_T, so, _decision(cfg=cfg), _risk_passed(), f, realized, _account())
        assert trail.realized_pnl_this_bar == pytest.approx(85.5)


# ---------------------------------------------------------------------------
# Test 7: Risk triggered fields correctly set
# ---------------------------------------------------------------------------

class TestRiskTriggeredFields:
    def test_risk_triggered_sets_rule_and_final_action(self, cfg):
        builder = DecisionTrailBuilder(_SYMBOL, cfg)
        builder.record_state("Surging_Up")
        so = _state_output()
        dec = _decision(action=DecisionAction.OPEN_LONG, cfg=cfg)
        risk = _risk_triggered("single_trade_max_loss")

        trail = builder.build(_T, so, dec, risk, None, None, _account())

        assert trail.risk_check_passed is False
        assert trail.risk_triggered == "single_trade_max_loss"
        assert trail.risk_details == "risk rule single_trade_max_loss fired"
        # final_action must use force_action (CLOSE), not proposed (OPEN_LONG)
        assert trail.final_action == "close"
        assert trail.proposed_action == "open_long"

    def test_risk_passed_has_none_triggered(self, cfg):
        builder = DecisionTrailBuilder(_SYMBOL, cfg)
        builder.record_state("Coiling")
        trail = builder.build(
            _T, _state_output(), _decision(cfg=cfg), _risk_passed(), None, None, _account()
        )
        assert trail.risk_triggered is None
        assert trail.risk_check_passed is True


# ---------------------------------------------------------------------------
# Test 8: DecisionReplayer.replay() re-runs decisions correctly
# ---------------------------------------------------------------------------

def _make_trail(
    state: str,
    prev_state: Optional[str],
    cfg: DecisionConfig,
    proposed: str = "open_long",
    final: str = "open_long",
) -> DecisionTrail:
    return DecisionTrail(
        time=_T,
        symbol=_SYMBOL,
        config_hash=cfg.config_hash,
        using_claude_default=True,
        current_state=state,
        previous_state=prev_state,
        state_history=[state],
        state_reason="test",
        close=60_000.0,
        sigma_p_24h=0.15,
        H=0.45,
        TF=None,
        OI=None,
        funding_rate=0.0001,
        LV=None,
        absorption_ratio=None,
        OI_hurst=None,
        feature_completeness=0.44,
        nav_usdt=10_000.0,
        position_side=None,
        position_size_usdt=0.0,
        unrealized_pnl_usdt=0.0,
        realized_pnl_usdt=0.0,
        proposed_action=proposed,
        final_action=final,
        rule_id="surging_up_from_coiling",
        risk_triggered=None,
        risk_details="ok",
        fill_price=None,
        fill_size_usdt=None,
        fill_fee_usdt=None,
        realized_pnl_this_bar=None,
        risk_check_passed=True,
    )


class TestDecisionReplayer:
    def test_replay_runs_correctly(self, cfg):
        replayer = DecisionReplayer(cfg)
        trail = _make_trail("Surging_Up", "Coiling", cfg)
        result = replayer.replay([trail])

        assert len(result) == 1
        new_trail = result[0]
        # Surging_Up from Coiling → open_long (per YAML rules)
        assert new_trail.proposed_action == "open_long"
        assert new_trail.current_state == "Surging_Up"

    def test_replayer_with_different_config_produces_different_decisions(self, cfg):
        """Config with different rules should change decisions."""
        import copy
        from decision.config import (
            DecisionRule,
            PositionConfig,
            AccountConfig,
            RiskConfig,
            ExecutionConfig,
        )

        # Create an alternative config where Surging_Up always holds
        alt_rules = [
            DecisionRule(
                id="everything_hold",
                current_state="*",
                previous_state="*",
                previous_state_pattern=None,
                action="hold",
                position_multiplier=None,
                notes="alt config: always hold",
            )
        ]
        alt_cfg = DecisionConfig(
            version="alt-0.1",
            config_hash="alt0000deadbeef0",
            rules=alt_rules,
            position=cfg.position,
            account=cfg.account,
            risk=cfg.risk,
            execution=cfg.execution,
            weekly_report_dir=cfg.weekly_report_dir,
        )

        trail = _make_trail("Surging_Up", "Coiling", cfg, proposed="open_long", final="open_long")

        # Replay with original config → open_long
        orig_replayer = DecisionReplayer(cfg)
        orig_result = orig_replayer.replay([trail])
        assert orig_result[0].proposed_action == "open_long"

        # Replay with alt config → hold
        alt_replayer = DecisionReplayer(alt_cfg)
        alt_result = alt_replayer.replay([trail])
        assert alt_result[0].proposed_action == "hold"

    # -------------------------------------------------------------------
    # Test 9: Replayer with different config produces different decisions
    # (already covered in test above; adding explicit assertion test)
    # -------------------------------------------------------------------
    def test_different_config_changes_rule_id(self, cfg):
        from decision.config import DecisionRule, DecisionConfig

        alt_rules = [
            DecisionRule(
                id="alt_open_short",
                current_state="Surging_Up",
                previous_state="Coiling",
                previous_state_pattern=None,
                action="open_short",
                position_multiplier=1.0,
                notes="alt: inverse",
            )
        ]
        alt_cfg = DecisionConfig(
            version="alt-0.2",
            config_hash="alt0002deadbeef0",
            rules=alt_rules,
            position=cfg.position,
            account=cfg.account,
            risk=cfg.risk,
            execution=cfg.execution,
            weekly_report_dir=cfg.weekly_report_dir,
        )

        trail = _make_trail("Surging_Up", "Coiling", cfg)
        replayer = DecisionReplayer(alt_cfg)
        result = replayer.replay([trail])

        assert result[0].proposed_action == "open_short"
        assert result[0].rule_id == "alt_open_short"

    # -------------------------------------------------------------------
    # Test 10: Replay output has same length as input
    # -------------------------------------------------------------------
    def test_replay_same_length_as_input(self, cfg):
        replayer = DecisionReplayer(cfg)
        trails = [
            _make_trail("Coiling", None, cfg, "no_action", "no_action"),
            _make_trail("Surging_Up", "Coiling", cfg, "open_long", "open_long"),
            _make_trail("Drifting_Charged", "Surging_Up", cfg, "hold", "hold"),
            _make_trail("Drifting_Calm", "Surging_Up", cfg, "close", "close"),
            _make_trail("Coiling", "Drifting_Calm", cfg, "no_action", "no_action"),
        ]
        result = replayer.replay(trails)
        assert len(result) == len(trails) == 5

    def test_replay_empty_list_returns_empty(self, cfg):
        replayer = DecisionReplayer(cfg)
        assert replayer.replay([]) == []


# ---------------------------------------------------------------------------
# Test 11: DecisionTrail.state_none_reason populated from StateOutput.none_reason
# ---------------------------------------------------------------------------

class TestDecisionTrailNoneReason:
    """Verify state_none_reason is set correctly for all three None causes."""

    def test_state_none_reason_cold_start(self, cfg):
        builder = DecisionTrailBuilder(_SYMBOL, cfg)
        builder.record_state(None)
        so = _state_output(state=None, prev_state=None, none_reason="cold_start")
        so.is_cold_start = True
        trail = builder.build(_T, so, _decision(action=DecisionAction.NO_ACTION, rule_id="none:cold_start", cfg=cfg),
                              _risk_passed(), None, None, _account())
        assert trail.state_none_reason == "cold_start"

    def test_state_none_reason_missing_data(self, cfg):
        builder = DecisionTrailBuilder(_SYMBOL, cfg)
        builder.record_state(None)
        so = _state_output(state=None, prev_state=None, none_reason="missing_data")
        so.is_cold_start = False
        trail = builder.build(_T, so, _decision(action=DecisionAction.NO_ACTION, rule_id="none:missing_data", cfg=cfg),
                              _risk_passed(), None, None, _account())
        assert trail.state_none_reason == "missing_data"

    def test_state_none_reason_no_match(self, cfg):
        builder = DecisionTrailBuilder(_SYMBOL, cfg)
        builder.record_state(None)
        so = _state_output(state=None, prev_state=None, none_reason="no_match")
        so.is_cold_start = False
        trail = builder.build(_T, so, _decision(action=DecisionAction.NO_ACTION, rule_id="none:no_match", cfg=cfg),
                              _risk_passed(), None, None, _account())
        assert trail.state_none_reason == "no_match"

    def test_state_none_reason_none_when_state_active(self, cfg):
        builder = DecisionTrailBuilder(_SYMBOL, cfg)
        builder.record_state("Coiling")
        so = _state_output(state="Coiling", prev_state=None, none_reason=None)
        trail = builder.build(_T, so, _decision(cfg=cfg), _risk_passed(), None, None, _account())
        assert trail.state_none_reason is None
