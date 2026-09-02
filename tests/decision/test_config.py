"""Tests for decision config loader — Wave 1."""

from pathlib import Path

import pytest

from decision.config import (
    AccountConfig,
    DecisionConfig,
    ExecutionConfig,
    PositionConfig,
    RiskConfig,
    _validate,
    load_config,
)

CONFIG_PATH = Path(__file__).parent.parent.parent / "configs" / "sel-decision-rules.yaml"


# ---------------------------------------------------------------------------
# 1. load_config() loads the actual YAML without errors
# ---------------------------------------------------------------------------
def test_load_config_no_errors():
    cfg = load_config(CONFIG_PATH)
    assert isinstance(cfg, DecisionConfig)


# ---------------------------------------------------------------------------
# 2. Config hash is a 16-char hex string
# ---------------------------------------------------------------------------
def test_config_hash_format():
    cfg = load_config(CONFIG_PATH)
    assert len(cfg.config_hash) == 16
    assert all(c in "0123456789abcdef" for c in cfg.config_hash)


# ---------------------------------------------------------------------------
# 3. All 13 rules are loaded with correct fields
# ---------------------------------------------------------------------------
def test_all_13_rules_loaded():
    cfg = load_config(CONFIG_PATH)
    assert len(cfg.rules) == 13, f"Expected 13 rules, got {len(cfg.rules)}: {[r.id for r in cfg.rules]}"
    for rule in cfg.rules:
        assert rule.id
        assert rule.current_state
        assert rule.action in {"open_long", "open_short", "close", "hold", "no_action"}


# ---------------------------------------------------------------------------
# 4. find_rule("Cascade", "Coiling") → cascade_force_close
# ---------------------------------------------------------------------------
def test_cascade_force_close():
    cfg = load_config(CONFIG_PATH)
    rule = cfg.find_rule("Cascade", "Coiling")
    assert rule is not None
    assert rule.id == "cascade_force_close"
    assert rule.action == "close"
    assert rule.position_multiplier == 0.0


# ---------------------------------------------------------------------------
# 5. find_rule("Surging_Up", "Coiling") → surging_up_from_coiling
# ---------------------------------------------------------------------------
def test_surging_up_from_coiling():
    cfg = load_config(CONFIG_PATH)
    rule = cfg.find_rule("Surging_Up", "Coiling")
    assert rule is not None
    assert rule.id == "surging_up_from_coiling"
    assert rule.action == "open_long"
    assert rule.position_multiplier == 1.0


# ---------------------------------------------------------------------------
# 6. find_rule("Surging_Up", "Drifting_Calm") → surging_up_not_from_coiling (regex)
# ---------------------------------------------------------------------------
def test_surging_up_not_from_coiling_regex():
    cfg = load_config(CONFIG_PATH)
    rule = cfg.find_rule("Surging_Up", "Drifting_Calm")
    assert rule is not None
    assert rule.id == "surging_up_not_from_coiling"
    assert rule.action == "no_action"


# ---------------------------------------------------------------------------
# 7. find_rule("Drifting_Calm", "Surging_Up") → drifting_calm_after_surging_close
# ---------------------------------------------------------------------------
def test_drifting_calm_after_surging_close():
    cfg = load_config(CONFIG_PATH)
    rule = cfg.find_rule("Drifting_Calm", "Surging_Up")
    assert rule is not None
    assert rule.id == "drifting_calm_after_surging_close"
    assert rule.action == "close"
    assert rule.position_multiplier == 0.0


# ---------------------------------------------------------------------------
# 8. find_rule("Drifting_Calm", "Coiling") → drifting_calm_default_hold (wildcard)
# ---------------------------------------------------------------------------
def test_drifting_calm_default_hold_wildcard():
    cfg = load_config(CONFIG_PATH)
    rule = cfg.find_rule("Drifting_Calm", "Coiling")
    assert rule is not None
    assert rule.id == "drifting_calm_default_hold"
    assert rule.action == "hold"
    assert rule.position_multiplier is None


# ---------------------------------------------------------------------------
# 9. Validation fails on missing required section
# ---------------------------------------------------------------------------
def test_validation_fails_missing_section():
    incomplete = {
        "meta": {"version": "0.1"},
        "decision_matrix": [],
        "position": {},
        # "account" missing
        "risk": {},
    }
    with pytest.raises(ValueError, match="missing required sections"):
        _validate(incomplete)


# ---------------------------------------------------------------------------
# 10. Validation fails on unknown action
# ---------------------------------------------------------------------------
def test_validation_fails_unknown_action():
    bad_data = {
        "meta": {"version": "0.1"},
        "decision_matrix": [{"id": "bad_rule", "current_state": "Coiling", "action": "fly_away"}],
        "position": {},
        "account": {},
        "risk": {},
    }
    with pytest.raises(ValueError, match="Unknown action"):
        _validate(bad_data)


# ---------------------------------------------------------------------------
# 11. PositionConfig, AccountConfig, RiskConfig, ExecutionConfig all parsed correctly
# ---------------------------------------------------------------------------
def test_position_config_parsed():
    cfg = load_config(CONFIG_PATH)
    pos = cfg.position
    assert isinstance(pos, PositionConfig)
    assert pos.base_size_pct == pytest.approx(0.20)
    assert pos.max_concurrent == 1
    assert pos.allow_add_to_position is False


def test_account_config_parsed():
    cfg = load_config(CONFIG_PATH)
    acc = cfg.account
    assert isinstance(acc, AccountConfig)
    assert acc.initial_nav_usdt == pytest.approx(10000.0)
    assert acc.currency == "USDT"


def test_risk_config_parsed():
    cfg = load_config(CONFIG_PATH)
    risk = cfg.risk
    assert isinstance(risk, RiskConfig)
    assert risk.max_loss_per_trade_pct == pytest.approx(0.03)
    assert risk.max_daily_drawdown_pct == pytest.approx(0.05)
    assert risk.consecutive_loss_stop == 3
    assert risk.signal_lag_max_hours == 2
    assert risk.cascade_always_overrides is True


def test_execution_config_parsed():
    cfg = load_config(CONFIG_PATH)
    ex = cfg.execution
    assert isinstance(ex, ExecutionConfig)
    assert ex.maker_fee_pct == pytest.approx(0.0002)
    assert ex.taker_fee_pct == pytest.approx(0.0005)
    assert ex.slippage_open_pct == pytest.approx(0.0005)
    assert ex.slippage_close_pct == pytest.approx(0.0005)
    assert ex.assume_taker is True
