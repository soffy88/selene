"""
Tests for WeeklyReportGenerator — Wave 5.

All tests use synthetic trail data via make_trail().
No DB, no network, no async.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone, timedelta
from typing import Optional

import pytest

from paper_trading.trail import DecisionTrail
from reports.generator import WeeklyReportGenerator
from sel_engine.states.health import HealthMonitor, HealthReport
from sel_engine.states.schema import StateLabel, StateRecord

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

BASE_TIME = datetime(2026, 4, 21, 0, 0, tzinfo=timezone.utc)
WEEK_ISO = "2026-W17"
INITIAL_NAV = 10_000.0


def make_trail(
    offset_hours: int = 0,
    current_state: Optional[str] = "Coiling",
    previous_state: Optional[str] = "Drifting_Calm",
    final_action: str = "no_action",
    proposed_action: str = "no_action",
    nav_usdt: float = INITIAL_NAV,
    fill_price: Optional[float] = None,
    fill_size_usdt: Optional[float] = None,
    realized_pnl_this_bar: Optional[float] = None,
    risk_triggered: Optional[str] = None,
    config_hash: str = "abc123def456abcd",
    using_claude_default: bool = True,
    close: Optional[float] = 30_000.0,
    rule_id: str = "coiling_default_no_action",
) -> DecisionTrail:
    """Create a synthetic DecisionTrail for testing."""
    t = BASE_TIME + timedelta(hours=offset_hours)
    return DecisionTrail(
        time=t,
        symbol="BTCUSDT",
        config_hash=config_hash,
        using_claude_default=using_claude_default,
        current_state=current_state,
        previous_state=previous_state,
        state_history=[previous_state or "None", current_state or "None"],
        state_reason="test",
        close=close,
        sigma_p_24h=0.02,
        H=0.5,
        TF=0.6,
        OI=1_000_000.0,
        funding_rate=0.0001,
        LV=None,
        absorption_ratio=None,
        OI_hurst=None,
        feature_completeness=0.75,
        nav_usdt=nav_usdt,
        position_side=None,
        position_size_usdt=0.0,
        unrealized_pnl_usdt=0.0,
        realized_pnl_usdt=0.0,
        proposed_action=proposed_action,
        final_action=final_action,
        rule_id=rule_id,
        risk_triggered=risk_triggered,
        risk_details="",
        fill_price=fill_price,
        fill_size_usdt=fill_size_usdt,
        fill_fee_usdt=None,
        realized_pnl_this_bar=realized_pnl_this_bar,
        risk_check_passed=risk_triggered is None,
    )


def _make_health_report(
    state_counts: Optional[dict[str, int]] = None,
    warnings: Optional[list[str]] = None,
) -> HealthReport:
    """Build a minimal HealthReport for tests."""
    all_states = [
        "Coiling", "Surging_Up", "Surging_Down",
        "Drifting_Calm", "Drifting_Charged", "Critical", "Cascade",
    ]
    counts = {s: 0 for s in all_states}
    if state_counts:
        counts.update(state_counts)
    total = sum(counts.values()) or 1
    rates = {s: counts[s] / total for s in all_states}
    return HealthReport(
        period_start=BASE_TIME,
        period_end=BASE_TIME + timedelta(days=7),
        total_bars=168,
        cold_start_bars=0,
        active_bars=total,
        state_counts=counts,
        state_rates=rates,
        legal_transition_rate=0.873,
        illegal_transition_types={"CASCADE->Surging_Up": 2},
        avg_dwell_times={s: 1.0 for s in all_states},
        cascade_cooling_events=1,
        in_healthy_range=False,
        health_warnings=warnings or [],
    )


def _make_week_of_trails() -> list[DecisionTrail]:
    """Build 168 synthetic trails (one per hour for a week)."""
    trails = []
    state_cycle = [
        "Coiling", "Coiling", "Surging_Up", "Drifting_Calm", "Drifting_Calm",
        "Cascade", "Drifting_Charged",
    ]
    for i in range(168):
        state = state_cycle[i % len(state_cycle)]
        prev = state_cycle[(i - 1) % len(state_cycle)]
        action = "hold"
        pnl = None
        nav = INITIAL_NAV + i * 0.85  # slight drift
        risk = None
        fill_price = None
        if state == "Surging_Up" and i % 7 == 2:
            action = "open_long"
            fill_price = 30_000.0
        elif state == "Cascade":
            action = "close"
            pnl = -100.0 if i % 3 == 0 else 50.0
            risk = "cascade" if i % 6 == 0 else None
        trails.append(
            make_trail(
                offset_hours=i,
                current_state=state,
                previous_state=prev,
                final_action=action,
                proposed_action=action,
                nav_usdt=nav,
                fill_price=fill_price,
                realized_pnl_this_bar=pnl,
                risk_triggered=risk,
                close=30_000.0 + i * 2,
            )
        )
    return trails


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture()
def generator(tmp_path):
    return WeeklyReportGenerator(), str(tmp_path)


@pytest.fixture()
def week_trails():
    return _make_week_of_trails()


@pytest.fixture()
def health(week_trails):
    counts = {}
    for t in week_trails:
        if t.current_state:
            counts[t.current_state] = counts.get(t.current_state, 0) + 1
    return _make_health_report(
        state_counts=counts,
        warnings=["Coiling rate 0.3571 below expected [0.10, 0.25]"],
    )


# ──────────────────────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────────────────────

class TestGenerateBasic:
    """Test 1: generate() returns a file path that exists."""

    def test_returns_existing_file(self, tmp_path, week_trails, health):
        gen = WeeklyReportGenerator()
        path = gen.generate(week_trails, health, WEEK_ISO, str(tmp_path))
        assert os.path.isfile(path), f"Expected file at {path}"


class TestSectionHeaders:
    """Test 2: Report contains all 9 section headers."""

    def test_all_nine_sections_present(self, tmp_path, week_trails, health):
        gen = WeeklyReportGenerator()
        path = gen.generate(week_trails, health, WEEK_ISO, str(tmp_path))
        content = open(path).read()
        for i in range(1, 10):
            assert f"## {i}." in content, f"Missing section ## {i}."


class TestConfigHash:
    """Test 3: Config hash appears in the report."""

    def test_config_hash_in_report(self, tmp_path, week_trails, health):
        gen = WeeklyReportGenerator()
        path = gen.generate(week_trails, health, WEEK_ISO, str(tmp_path))
        content = open(path).read()
        assert "abc123def456abcd" in content


class TestStateDistribution:
    """Test 4: State distribution table correct — all states present even at 0%."""

    def test_all_states_in_table(self, tmp_path, week_trails, health):
        gen = WeeklyReportGenerator()
        path = gen.generate(week_trails, health, WEEK_ISO, str(tmp_path))
        content = open(path).read()
        for state in [
            "Coiling", "Surging_Up", "Surging_Down",
            "Drifting_Calm", "Drifting_Charged", "Critical", "Cascade",
        ]:
            assert state in content, f"State {state} missing from distribution table"

    def test_zero_count_states_present(self, tmp_path, health):
        """States with 0 occurrences should still appear."""
        # Only Coiling trails — other states should show 0%
        single_state_trails = [
            make_trail(offset_hours=i, current_state="Coiling", final_action="no_action")
            for i in range(10)
        ]
        small_health = _make_health_report(state_counts={"Coiling": 10})
        gen = WeeklyReportGenerator()
        path = gen.generate(single_state_trails, small_health, WEEK_ISO, str(tmp_path))
        content = open(path).read()
        # Surging_Up should appear with count 0
        assert "Surging_Up" in content
        assert "Critical" in content


class TestPnLSection:
    """Test 5: PnL section shows correct week/starting/ending NAV."""

    def test_nav_values_in_report(self, tmp_path, week_trails, health):
        gen = WeeklyReportGenerator()
        path = gen.generate(week_trails, health, WEEK_ISO, str(tmp_path))
        content = open(path).read()
        # Starting NAV is INITIAL_NAV = 10,000.00
        assert "10,000.00" in content
        # Ending NAV is the last trail's nav_usdt
        ending_nav = week_trails[-1].nav_usdt
        assert f"{ending_nav:,.2f}" in content

    def test_pnl_calculated_from_realized(self, tmp_path, health):
        """Week PnL should sum only realized_pnl_this_bar fields."""
        trails = [
            make_trail(offset_hours=0, nav_usdt=10_000.0, realized_pnl_this_bar=None),
            make_trail(offset_hours=1, nav_usdt=10_050.0, realized_pnl_this_bar=50.0),
            make_trail(offset_hours=2, nav_usdt=10_030.0, realized_pnl_this_bar=-20.0),
            make_trail(offset_hours=3, nav_usdt=10_030.0, realized_pnl_this_bar=None),
        ]
        gen = WeeklyReportGenerator()
        path = gen.generate(trails, _make_health_report(), WEEK_ISO, str(tmp_path))
        content = open(path).read()
        # Net PnL = 50 - 20 = 30
        assert "+$30.00" in content


class TestRiskEvents:
    """Test 6: Risk events table lists triggered rules."""

    def test_risk_rules_listed(self, tmp_path, health):
        trails = [
            make_trail(offset_hours=0, risk_triggered=None),
            make_trail(offset_hours=1, risk_triggered="cascade"),
            make_trail(offset_hours=2, risk_triggered="cascade"),
            make_trail(offset_hours=3, risk_triggered="consecutive_loss_stop"),
        ]
        gen = WeeklyReportGenerator()
        path = gen.generate(trails, _make_health_report(), WEEK_ISO, str(tmp_path))
        content = open(path).read()
        assert "cascade" in content
        assert "consecutive_loss_stop" in content


class TestTopLosses:
    """Test 7: Top 5 losses section present and sorted."""

    def test_losses_sorted_worst_first(self, tmp_path, health):
        trails = [
            make_trail(offset_hours=0, final_action="close", realized_pnl_this_bar=-50.0),
            make_trail(offset_hours=1, final_action="close", realized_pnl_this_bar=-200.0),
            make_trail(offset_hours=2, final_action="close", realized_pnl_this_bar=-10.0),
            make_trail(offset_hours=3, final_action="close", realized_pnl_this_bar=-75.0),
            make_trail(offset_hours=4, final_action="close", realized_pnl_this_bar=-300.0),
            make_trail(offset_hours=5, final_action="close", realized_pnl_this_bar=-5.0),
        ]
        gen = WeeklyReportGenerator()
        path = gen.generate(trails, _make_health_report(), WEEK_ISO, str(tmp_path))
        content = open(path).read()
        assert "Failure Cases" in content
        # -300 should appear before -200 in the file (sorted worst-first)
        idx_300 = content.find("-$300.00")
        idx_200 = content.find("-$200.00")
        assert idx_300 != -1, "-$300.00 not found in report"
        assert idx_200 != -1, "-$200.00 not found in report"
        assert idx_300 < idx_200, "Losses not sorted worst-first"

    def test_top5_section_present(self, tmp_path, week_trails, health):
        gen = WeeklyReportGenerator()
        path = gen.generate(week_trails, health, WEEK_ISO, str(tmp_path))
        content = open(path).read()
        assert "Failure Cases" in content
        assert "Top 5 Losses" in content


class TestPriceOutcomes:
    """Test 8: Price outcomes section present with low-power warning when N < 30."""

    def test_price_outcomes_section_present(self, tmp_path, week_trails, health):
        gen = WeeklyReportGenerator()
        path = gen.generate(week_trails, health, WEEK_ISO, str(tmp_path))
        content = open(path).read()
        assert "Price Outcomes After State" in content

    def test_low_power_warning_when_n_lt_30(self, tmp_path, health):
        """With fewer than 30 active-state bars, low-power note must appear."""
        trails = [
            make_trail(offset_hours=i, current_state="Coiling", close=30_000.0 + i)
            for i in range(10)  # only 10 bars
        ]
        gen = WeeklyReportGenerator()
        path = gen.generate(trails, _make_health_report(), WEEK_ISO, str(tmp_path))
        content = open(path).read()
        assert "N < 30" in content or "statistical power is very low" in content


class TestHypothesisHealth:
    """Test 9: sel hypothesis health section present."""

    def test_hypothesis_section_present(self, tmp_path, week_trails, health):
        gen = WeeklyReportGenerator()
        path = gen.generate(week_trails, health, WEEK_ISO, str(tmp_path))
        content = open(path).read()
        assert "sel Hypothesis Health" in content

    def test_watch_or_pass_present(self, tmp_path, week_trails, health):
        gen = WeeklyReportGenerator()
        path = gen.generate(week_trails, health, WEEK_ISO, str(tmp_path))
        content = open(path).read()
        assert "WATCH" in content or "PASS" in content


class TestFileName:
    """Test 10: Report file named correctly (YYYY-WW.md format)."""

    def test_file_name_format(self, tmp_path, week_trails, health):
        gen = WeeklyReportGenerator()
        path = gen.generate(week_trails, health, "2026-W17", str(tmp_path))
        filename = os.path.basename(path)
        assert re.match(r"^\d{4}-W\d{2}\.md$", filename), f"Bad filename: {filename}"
        assert filename == "2026-W17.md"


class TestMissingFillPrice:
    """Test 11: Missing fill_price fields handled gracefully (no crash)."""

    def test_no_crash_with_none_fill_price(self, tmp_path, health):
        trails = [
            make_trail(offset_hours=i, fill_price=None, fill_size_usdt=None)
            for i in range(20)
        ]
        gen = WeeklyReportGenerator()
        # Should not raise
        path = gen.generate(trails, _make_health_report(), WEEK_ISO, str(tmp_path))
        assert os.path.isfile(path)


class TestEmptyTrails:
    """Test 12: Empty trail list → generates report with 'No data' sections."""

    def test_empty_trails_no_crash(self, tmp_path):
        health = _make_health_report()
        gen = WeeklyReportGenerator()
        path = gen.generate([], health, WEEK_ISO, str(tmp_path))
        assert os.path.isfile(path)
        content = open(path).read()
        assert "No data" in content

    def test_empty_trails_has_header(self, tmp_path):
        gen = WeeklyReportGenerator()
        path = gen.generate([], _make_health_report(), WEEK_ISO, str(tmp_path))
        content = open(path).read()
        assert f"Selene Weekly Report — {WEEK_ISO}" in content
