"""
Tests for v2.1 §2.1 Critical entry logic.

Covers:
- A/B/C tristate truth table
- Path 1 (A_full + B or C) and Path 2 (A_partial + B + C)
- None data never triggers Critical alone
- A_full vs A_partial semantics
"""
import pytest
from datetime import datetime, timezone
from typing import Optional

from sel_v2.states.schema import BarFeatures
from sel_v2.states.critical_logic import (
    evaluate_critical_entry, CriticalConditions,
    _eval_a1, _eval_a2, _eval_b, _eval_c, _and, _or,
)

_TS = datetime(2025, 1, 1, tzinfo=timezone.utc)


def _feat(
    sigma_pctile: float = 0.95,
    sigma_monotone: Optional[bool] = True,
    entropy_variance_rising: Optional[bool] = None,
    hawkes_br: Optional[float] = None,
    hawkes_threshold: float = 0.85,
    tda_l1_pctile: Optional[float] = None,
    tda_l1_monotone: Optional[bool] = None,
    tda_threshold: float = 0.000097,
) -> BarFeatures:
    return BarFeatures(
        timestamp=_TS, bar_index=600, close=50000.0, log_price=10.8,
        sigma_4h=0.02, sigma_pctile=sigma_pctile,
        sigma_monotone_3bar=sigma_monotone,
        price_breakout_up=None, price_breakout_down=None,
        entropy_variance_rising=entropy_variance_rising,
        hawkes_br=hawkes_br, hawkes_br_threshold=hawkes_threshold,
        tda_l1=None, tda_l1_pctile=tda_l1_pctile,
        tda_l1_threshold=tda_threshold, tda_l1_monotone_3bar=tda_l1_monotone,
        cold_start=False,
    )


# ── Tristate helpers ──────────────────────────────────────────────────────────

def test_and_truth_table():
    assert _and(True, True) is True
    assert _and(True, False) is False
    assert _and(False, True) is False
    assert _and(False, False) is False
    assert _and(True, None) is None
    assert _and(None, True) is None
    assert _and(None, None) is None
    assert _and(False, None) is False


def test_or_truth_table():
    assert _or(True, True) is True
    assert _or(True, False) is True
    assert _or(False, True) is True
    assert _or(False, False) is False
    assert _or(True, None) is True
    assert _or(None, True) is True
    assert _or(None, None) is None
    assert _or(False, None) is None


# ── A1 condition ──────────────────────────────────────────────────────────────

def test_a1_true():
    f = _feat(sigma_pctile=0.95, sigma_monotone=True)
    assert _eval_a1(f) is True


def test_a1_false_sigma_too_low():
    f = _feat(sigma_pctile=0.85, sigma_monotone=True)
    assert _eval_a1(f) is False


def test_a1_false_not_monotone():
    f = _feat(sigma_pctile=0.95, sigma_monotone=False)
    assert _eval_a1(f) is False


def test_a1_none_monotone_unavailable():
    f = _feat(sigma_pctile=0.95, sigma_monotone=None)
    # σ level met but can't check monotone → None
    assert _eval_a1(f) is None


# ── A2 condition (STUB) ───────────────────────────────────────────────────────

def test_a2_always_none_in_wave3():
    f = _feat(entropy_variance_rising=None)
    assert _eval_a2(f) is None


def test_a2_true_when_entropy_available():
    f = _feat(entropy_variance_rising=True)
    assert _eval_a2(f) is True


def test_a2_false_when_entropy_not_rising():
    f = _feat(entropy_variance_rising=False)
    assert _eval_a2(f) is False


# ── B condition (Hawkes) ──────────────────────────────────────────────────────

def test_b_true():
    f = _feat(hawkes_br=0.90)
    assert _eval_b(f) is True


def test_b_false():
    f = _feat(hawkes_br=0.70)
    assert _eval_b(f) is False


def test_b_none_when_no_hawkes():
    f = _feat(hawkes_br=None)
    assert _eval_b(f) is None


def test_b_exact_threshold_is_false():
    f = _feat(hawkes_br=0.85, hawkes_threshold=0.85)
    # > threshold, not >=
    assert _eval_b(f) is False


def test_b_above_threshold_true():
    f = _feat(hawkes_br=0.851, hawkes_threshold=0.85)
    assert _eval_b(f) is True


# ── C condition (TDA) ─────────────────────────────────────────────────────────

def test_c_true():
    f = _feat(tda_l1_pctile=0.96, tda_l1_monotone=True)
    assert _eval_c(f) is True


def test_c_false_pctile_too_low():
    f = _feat(tda_l1_pctile=0.90, tda_l1_monotone=True)
    assert _eval_c(f) is False


def test_c_false_not_monotone():
    f = _feat(tda_l1_pctile=0.98, tda_l1_monotone=False)
    assert _eval_c(f) is False


def test_c_none_pctile_unavailable():
    f = _feat(tda_l1_pctile=None)
    assert _eval_c(f) is None


def test_c_none_monotone_unavailable():
    f = _feat(tda_l1_pctile=0.97, tda_l1_monotone=None)
    assert _eval_c(f) is None


# ── Full Critical entry truth table ──────────────────────────────────────────

def _eval(
    sigma_pctile=0.95, sigma_monotone=True,
    entropy_rising=None,
    hawkes_br=None,
    tda_pctile=None, tda_monotone=None,
) -> CriticalConditions:
    f = _feat(
        sigma_pctile=sigma_pctile,
        sigma_monotone=sigma_monotone,
        entropy_variance_rising=entropy_rising,
        hawkes_br=hawkes_br,
        tda_l1_pctile=tda_pctile,
        tda_l1_monotone=tda_monotone,
    )
    return evaluate_critical_entry(f)


# Path 2: A_partial + B + C → Critical
def test_path2_full():
    cc = _eval(hawkes_br=0.90, tda_pctile=0.97, tda_monotone=True)
    assert cc.a_partial is True
    assert cc.b is True
    assert cc.c is True
    assert cc.path2_met is True
    assert cc.met is True


# Path 1: A_full + (B or C) → Critical (requires entropy available)
def test_path1_with_entropy_and_b():
    cc = _eval(entropy_rising=True, hawkes_br=0.90, tda_pctile=None)
    assert cc.a_full is True
    assert cc.b is True
    assert cc.c is None
    # OR(b=True, c=None) = True → path1 fires
    assert cc.path1_met is True
    assert cc.met is True


def test_path1_with_entropy_and_c():
    cc = _eval(entropy_rising=True, hawkes_br=None, tda_pctile=0.97, tda_monotone=True)
    assert cc.a_full is True
    assert cc.b is None
    assert cc.c is True
    assert cc.path1_met is True
    assert cc.met is True


# Path 2 fails when B or C is missing
def test_path2_fails_missing_b():
    cc = _eval(hawkes_br=None, tda_pctile=0.97, tda_monotone=True)
    assert cc.a_partial is True
    assert cc.b is None
    assert cc.c is True
    assert cc.path2_met is False
    assert cc.met is False


def test_path2_fails_missing_c():
    cc = _eval(hawkes_br=0.90, tda_pctile=None)
    assert cc.path2_met is False
    assert cc.met is False


# σ too low → no Critical
def test_no_critical_sigma_low():
    cc = _eval(sigma_pctile=0.80, hawkes_br=0.90, tda_pctile=0.97, tda_monotone=True)
    assert cc.a1 is False
    assert cc.a_partial is False
    assert cc.met is False


# σ not monotone → no Critical
def test_no_critical_not_monotone():
    cc = _eval(sigma_monotone=False, hawkes_br=0.90, tda_pctile=0.97, tda_monotone=True)
    assert cc.a1 is False
    assert cc.met is False


# All None → no Critical (conservative)
def test_all_none_no_critical():
    cc = _eval(sigma_pctile=0.95, sigma_monotone=None, hawkes_br=None, tda_pctile=None)
    # a1=None, a_partial=None → no entry
    assert cc.met is False


# A_full vs A_partial semantics
def test_a_full_requires_entropy():
    # entropy=None → a_full=None (can never be full without entropy)
    cc = _eval(entropy_rising=None)
    assert cc.a_full is None


def test_a_full_true_when_entropy_available():
    cc = _eval(entropy_rising=True, sigma_pctile=0.95, sigma_monotone=True)
    assert cc.a_full is True
    assert cc.a_partial is False  # a_partial = A1 True AND A2 NOT True; A2=True here


def test_a_partial_when_entropy_false():
    # Even when entropy is explicitly False, A_partial can still be True
    # A_partial = A1 True AND A2 not True (includes False)
    cc = _eval(entropy_rising=False, sigma_pctile=0.95, sigma_monotone=True)
    assert cc.a1 is True
    assert cc.a2 is False
    assert cc.a_full is False
    assert cc.a_partial is True


# None tags populated correctly
def test_none_tags_wave3_typical():
    cc = _eval(hawkes_br=None, tda_pctile=None)
    assert "A2_entropy_variance" in cc.none_tags
    assert "B_hawkes_br" in cc.none_tags
    assert "C_tda_l1" in cc.none_tags
