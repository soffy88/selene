"""
Tests for sel v2.0 state condition functions.

Covers:
- 6 state condition functions (True/False/None tristate)
- STUB data → None handling
- Cascade OR logic
- Priority order + min-dwell integration
"""
import pytest
from datetime import datetime, timezone
from typing import Optional

from sel_v2.states.schema import BarFeatures, StateLabel, MIN_DWELL_BARS
from sel_v2.states.conditions import (
    check_cascade, check_critical, check_surging,
    check_coiling, check_drifting_charged, check_drifting_calm,
)
from sel_v2.states.priority import arbitrate

_TS = datetime(2025, 1, 1, tzinfo=timezone.utc)


def _feat(**kwargs) -> BarFeatures:
    defaults = dict(
        timestamp=_TS, bar_index=600, close=50000.0, log_price=10.8,
        sigma_4h=0.01, sigma_pctile=0.50,
        sigma_monotone_3bar=None,
        price_breakout_up=None, price_breakout_down=None,
        cold_start=False,
    )
    defaults.update(kwargs)
    return BarFeatures(**defaults)


# ── Cascade ───────────────────────────────────────────────────────────────────

def test_cascade_all_stub_returns_none():
    f = _feat(sigma_pctile=0.98)
    # lob_depth=None, liquidation=None, spread=None → OR(None,None,None) = None
    res = check_cascade(f)
    assert res.met is None
    assert "lob_depth" in res.none_conditions
    assert "liquidation_pulse" in res.none_conditions
    assert "cross_exchange_spread" in res.none_conditions


def test_cascade_false_when_sigma_low_and_others_false():
    f = _feat(sigma_pctile=0.50, lob_depth_pctile=0.50,
               liquidation_pulse=False, cross_exchange_spread=0.1)
    res = check_cascade(f)
    assert res.met is False


def test_cascade_true_when_liquidation_fires():
    f = _feat(liquidation_pulse=True)
    res = check_cascade(f)
    assert res.met is True


def test_cascade_true_when_spread_fires():
    f = _feat(cross_exchange_spread=0.6, liquidation_pulse=False, lob_depth_pctile=0.50)
    res = check_cascade(f)
    assert res.met is True


def test_cascade_true_when_sigma_plus_lob():
    f = _feat(sigma_pctile=0.98, lob_depth_pctile=0.03,
               liquidation_pulse=False, cross_exchange_spread=0.1)
    res = check_cascade(f)
    assert res.met is True


def test_cascade_sigma_extreme_but_lob_none_is_none():
    f = _feat(sigma_pctile=0.98, lob_depth_pctile=None,
               liquidation_pulse=None, cross_exchange_spread=None)
    res = check_cascade(f)
    assert res.met is None


# ── Critical ──────────────────────────────────────────────────────────────────

def test_critical_requires_sigma_above_90():
    f = _feat(sigma_pctile=0.85, sigma_monotone_3bar=True,
               hawkes_br=0.90, tda_l1_pctile=0.97, tda_l1_monotone_3bar=True)
    res = check_critical(f)
    assert res.met is False


def test_critical_path2_fires():
    f = _feat(sigma_pctile=0.95, sigma_monotone_3bar=True,
               hawkes_br=0.90,
               tda_l1_pctile=0.97, tda_l1_monotone_3bar=True)
    res = check_critical(f)
    assert res.met is True


def test_critical_all_stub_after_sigma_fail_is_false():
    f = _feat(sigma_pctile=0.80)
    res = check_critical(f)
    assert res.met is False


# ── Surging ───────────────────────────────────────────────────────────────────

def test_surging_price_breakout_and_sigma_jump_stubs_none():
    f = _feat(price_breakout_up=True, sigma_pctile=0.75)
    res = check_surging(f)
    # price+σ True but ofi=None, oi=None (both STUB) → met=None (conservative)
    assert res.met is None
    assert "ofi_90pct" in res.none_conditions
    assert "oi_acceleration" in res.none_conditions


def test_surging_fires_when_flow_available():
    f = _feat(price_breakout_up=True, sigma_pctile=0.75, oi_acceleration=True)
    res = check_surging(f)
    # price+σ True, oi_accel=True → met=True
    assert res.met is True


def test_surging_no_breakout_false():
    f = _feat(price_breakout_up=False, price_breakout_down=False, sigma_pctile=0.75)
    res = check_surging(f)
    assert res.met is False


def test_surging_sigma_too_low():
    f = _feat(price_breakout_up=True, sigma_pctile=0.50)
    res = check_surging(f)
    # sigma_jump=False → False
    assert res.met is False


# ── Coiling ───────────────────────────────────────────────────────────────────

def test_coiling_sigma_low_all_stubs_none():
    f = _feat(sigma_pctile=0.20)
    res = check_coiling(f)
    # sigma_low=True but entropy=None, oi=None, funding=None → met=None (conservative)
    assert res.met is None
    assert len(res.none_conditions) == 3


def test_coiling_fires_when_accumulation_available():
    f = _feat(sigma_pctile=0.20, oi_change_rate=0.01)
    res = check_coiling(f)
    # sigma_low=True, oi_positive=True, others STUB → met=True
    assert res.met is True


def test_coiling_sigma_high_false():
    f = _feat(sigma_pctile=0.60)
    res = check_coiling(f)
    assert res.met is False


def test_coiling_all_available_and_true():
    f = _feat(sigma_pctile=0.20, entropy_pctile=0.25,
               oi_change_rate=0.01, funding_pctile=0.50)
    res = check_coiling(f)
    assert res.met is True
    assert not res.none_conditions


def test_coiling_sigma_low_but_funding_extreme():
    f = _feat(sigma_pctile=0.20, entropy_pctile=0.25,
               oi_change_rate=0.01, funding_pctile=0.85)
    # funding_pctile=0.85 → funding_neutral=False → met=False
    res = check_coiling(f)
    assert res.met is False


# ── Drifting-Charged ──────────────────────────────────────────────────────────

def test_drifting_charged_sigma_mid_stubs_none():
    f = _feat(sigma_pctile=0.55)
    res = check_drifting_charged(f)
    # sigma_mid=True, oi=None, funding=None → accumulation=None → met=None
    assert res.met is None


def test_drifting_charged_oi_elevated():
    f = _feat(sigma_pctile=0.55, oi_change_rate_pctile=0.80)
    res = check_drifting_charged(f)
    assert res.met is True


def test_drifting_charged_sigma_too_high():
    f = _feat(sigma_pctile=0.85)
    res = check_drifting_charged(f)
    assert res.met is False


# ── Drifting-Calm ─────────────────────────────────────────────────────────────

def test_drifting_calm_sigma_mid():
    f = _feat(sigma_pctile=0.45)
    res = check_drifting_calm(f)
    assert res.met is True


def test_drifting_calm_sigma_too_high():
    f = _feat(sigma_pctile=0.70)
    res = check_drifting_calm(f)
    assert res.met is False


def test_drifting_calm_sigma_too_low():
    f = _feat(sigma_pctile=0.25)
    res = check_drifting_calm(f)
    assert res.met is False


# ── Priority arbitration ──────────────────────────────────────────────────────

def test_priority_cascade_beats_critical():
    from sel_v2.states.schema import ConditionResult
    results = {
        StateLabel.CASCADE: ConditionResult(True, [], [], {}),
        StateLabel.CRITICAL: ConditionResult(True, [], [], {}),
        StateLabel.SURGING: ConditionResult(False, [], [], {}),
        StateLabel.COILING: ConditionResult(False, [], [], {}),
        StateLabel.DRIFTING_CHARGED: ConditionResult(False, [], [], {}),
        StateLabel.DRIFTING_CALM: ConditionResult(True, [], [], {}),
    }
    state, changed = arbitrate(results, current_state=None, current_duration_4h=0)
    assert state == StateLabel.CASCADE


def test_priority_critical_beats_surging():
    from sel_v2.states.schema import ConditionResult
    results = {
        StateLabel.CASCADE: ConditionResult(False, [], [], {}),
        StateLabel.CRITICAL: ConditionResult(True, [], [], {}),
        StateLabel.SURGING: ConditionResult(True, [], [], {}),
        StateLabel.COILING: ConditionResult(False, [], [], {}),
        StateLabel.DRIFTING_CHARGED: ConditionResult(False, [], [], {}),
        StateLabel.DRIFTING_CALM: ConditionResult(False, [], [], {}),
    }
    # current=Surging, 10 bars dwell (min=2 met)
    state, _ = arbitrate(results, current_state=StateLabel.SURGING, current_duration_4h=10)
    assert state == StateLabel.CRITICAL


def test_cascade_instant_switch_ignores_min_dwell():
    """Cascade must trigger even if current state hasn't met its min dwell."""
    from sel_v2.states.schema import ConditionResult
    results = {
        StateLabel.CASCADE: ConditionResult(True, [], [], {}),
        StateLabel.CRITICAL: ConditionResult(False, [], [], {}),
        StateLabel.SURGING: ConditionResult(False, [], [], {}),
        StateLabel.COILING: ConditionResult(False, [], [], {}),
        StateLabel.DRIFTING_CHARGED: ConditionResult(False, [], [], {}),
        StateLabel.DRIFTING_CALM: ConditionResult(False, [], [], {}),
    }
    # current=Coiling with only 2 bars (min_dwell=6 not met)
    state, changed = arbitrate(results, current_state=StateLabel.COILING, current_duration_4h=2)
    assert state == StateLabel.CASCADE
    assert changed is True


def test_min_dwell_prevents_premature_exit():
    """Coiling needs 6 bars before switching; at 3 bars should stay."""
    from sel_v2.states.schema import ConditionResult
    results = {
        StateLabel.CASCADE: ConditionResult(False, [], [], {}),
        StateLabel.CRITICAL: ConditionResult(False, [], [], {}),
        StateLabel.SURGING: ConditionResult(True, [], [], {}),
        StateLabel.COILING: ConditionResult(True, [], [], {}),
        StateLabel.DRIFTING_CHARGED: ConditionResult(False, [], [], {}),
        StateLabel.DRIFTING_CALM: ConditionResult(True, [], [], {}),
    }
    # current=Coiling with 3 bars (min_dwell=6 not met) → can't switch to Surging
    state, changed = arbitrate(results, current_state=StateLabel.COILING, current_duration_4h=3)
    assert state == StateLabel.COILING
    assert changed is False


# ── Min dwell values ──────────────────────────────────────────────────────────

def test_min_dwell_values():
    assert MIN_DWELL_BARS[StateLabel.COILING] == 6
    assert MIN_DWELL_BARS[StateLabel.SURGING] == 2
    assert MIN_DWELL_BARS[StateLabel.DRIFTING_CALM] == 8
    assert MIN_DWELL_BARS[StateLabel.DRIFTING_CHARGED] == 4
    assert MIN_DWELL_BARS[StateLabel.CRITICAL] == 1
    assert MIN_DWELL_BARS[StateLabel.CASCADE] == 0
