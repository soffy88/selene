"""
Tests for WeeklyGenerator — v2.0 §26.2 field completeness and structure.
"""

from __future__ import annotations

import os
import tempfile

from sel_v2.reports.weekly_generator import WeeklyGenerator


def _make_full_data(week: str = "2026-W18") -> dict:
    return {
        "period_start": "2026-04-27",
        "period_end": "2026-05-04",
        "state_distribution": {
            "state_hours": {
                "Drifting_Calm": 162.0,
                "Critical": 6.0,
                "Coiling": 0.0,
                "Surging": 0.0,
                "Drifting_Charged": 0.0,
                "Cascade": 0.0,
            },
            "state_switches": {"Critical": 1},
            "transitions": [
                {
                    "from_state": "Drifting_Calm",
                    "to_state": "Critical",
                    "timestamp": "2026-04-29 12:00",
                    "trigger_condition": "σ > 90 pctile",
                },
            ],
            "total_hours": 168.0,
        },
        "cusum_stats": {
            "cusum_mid_triggers": 2,
            "cusum_mid_avg_peak": 0.034,
            "cusum_mid_h_range": (0.028, 0.041),
            "cusum_short_triggers": 12,
            "cusum_short_type_a_pct": 58.3,
            "cusum_short_type_b_pct": 41.7,
        },
        "inverse_vocab_stats": {
            "vocab_counts": {"Sweep": 3, "Absorption": 1},
        },
        "strategy1_perf": {
            "entries": 1,
            "exits": 1,
            "avg_hold_hours": 36.0,
            "time_stop_triggers": 0,
            "total_pnl_usdt": 12.5,
            "win_rate_pct": 100.0,
            "avg_win_usdt": 12.5,
            "avg_loss_usdt": None,
            "max_single_drawdown_usdt": -4.2,
            "critical_reduce_count": 0,
            "cascade_exit_count": 0,
            "stop_loss_count": 0,
        },
        "strategy2_perf": {
            "entries": 0,
            "exits": 0,
        },
        "coordination_events": {
            "reverse_hedge_count": 0,
            "independent_same_direction_count": 1,
        },
        "anomalies": {
            "data_gaps": [],
            "api_errors": [],
            "decision_anomalies": [],
        },
        "account_state": {
            "sub_account_1_usdt": 8_000.0,
            "sub_account_2_usdt": 2_000.0,
            "total_pct_change": 0.12,
        },
        "observation_tools": {
            "tool_triggers": {"B1": 2, "I1": 1},
            "tool_notes": {},
        },
    }


# ── Structure tests ───────────────────────────────────────────────────────────


def test_generate_markdown_returns_string():
    gen = WeeklyGenerator()
    md = gen.generate_markdown("2026-W18", _make_full_data())
    assert isinstance(md, str)
    assert len(md) > 500


def test_report_has_9_sections():
    """All 9 §26.2 sections must appear in the output."""
    gen = WeeklyGenerator()
    md = gen.generate_markdown("2026-W18", _make_full_data())

    required_headings = [
        "## 状態機統計",
        "## CUSUM 触発統計",
        "## 反推詞彙出現統計",
        "## 策略 1 表現",
        "## 策略 2 表現",
        "## 協調層事件",
        "## 異常事件日誌",
        "## 週末口座状態",
        "## Observation-only ツール触発統計",
    ]
    for heading in required_headings:
        assert heading in md, f"Missing section: {heading}"


def test_report_has_manual_placeholder_sections():
    """Three manual placeholder sections must appear (v2.0 §26.2)."""
    gen = WeeklyGenerator()
    md = gen.generate_markdown("2026-W18", _make_full_data())

    assert "## Wiki 視角" in md
    assert "## Claude 反馈" in md
    assert "## 調参決策記録" in md


def test_report_header_contains_week_iso():
    gen = WeeklyGenerator()
    md = gen.generate_markdown("2026-W18", _make_full_data())
    assert "2026-W18" in md


def test_report_header_contains_period():
    gen = WeeklyGenerator()
    data = _make_full_data()
    md = gen.generate_markdown("2026-W18", data)
    assert "2026-04-27" in md
    assert "2026-05-04" in md


def test_all_6_states_in_distribution_table():
    gen = WeeklyGenerator()
    md = gen.generate_markdown("2026-W18", _make_full_data())
    for state in ["Coiling", "Surging", "Drifting_Calm", "Drifting_Charged", "Critical", "Cascade"]:
        assert state in md, f"State {state} missing from distribution table"


def test_all_7_observation_tools_in_table():
    gen = WeeklyGenerator()
    md = gen.generate_markdown("2026-W18", _make_full_data())
    for tid in ["B1", "B2", "TDA2", "I1", "T2", "W2", "H3"]:
        assert tid in md, f"Tool {tid} missing from observation tools table"


def test_empty_data_does_not_crash():
    """Generator must not crash on completely empty data dict."""
    gen = WeeklyGenerator()
    md = gen.generate_markdown("2026-W99", {})
    assert isinstance(md, str)
    assert "## Wiki 視角" in md


def test_na_values_present_when_no_data():
    """Empty performance dicts should render as N/A."""
    gen = WeeklyGenerator()
    data = {"period_start": "2026-04-27", "period_end": "2026-05-04"}
    md = gen.generate_markdown("2026-W18", data)
    assert "N/A" in md


# ── File write tests ──────────────────────────────────────────────────────────


def test_write_to_disk_creates_file():
    with tempfile.TemporaryDirectory() as tmp:
        gen = WeeklyGenerator(output_dir=tmp)
        path = gen.generate("2026-W18", _make_full_data(), write_to_disk=True)
        assert os.path.isfile(path)
        assert path.endswith("2026-W18.md")


def test_written_file_content_matches_markdown():
    with tempfile.TemporaryDirectory() as tmp:
        gen = WeeklyGenerator(output_dir=tmp)
        path = gen.generate("2026-W18", _make_full_data(), write_to_disk=True)
        with open(path, encoding="utf-8") as f:
            content = f.read()
        assert "## 状態機統計" in content
        assert "## Wiki 視角" in content


def test_generate_sample_report_current_data_state():
    """
    Generate a sample report reflecting the current data state
    (large majority N/A — this is the expected state pre-paper-trading).
    """
    with tempfile.TemporaryDirectory() as tmp:
        gen = WeeklyGenerator(output_dir=tmp)
        # Minimal data representing current state (Wave 5, pre-paper)
        data = {
            "period_start": "2026-04-22",
            "period_end": "2026-04-29",
            "state_distribution": {
                "state_hours": {"Drifting_Calm": 163.5, "Critical": 4.5},
                "total_hours": 168.0,
            },
            "observation_tools": {
                "tool_triggers": {},
                "tool_notes": {
                    "B1": "rolling; no disagreements",
                    "B2": "Drifting-Charged never recognized (OI STUB)",
                    "TDA2": "running degraded (ripser available)",
                    "I1": "PE normal range",
                    "T2": "funding_rate STUB → NO_DATA",
                    "W2": "energy ratio within bounds",
                    "H3": "liquidation STUB → degraded mode",
                },
            },
        }
        path = gen.generate("2026-W18", data, write_to_disk=True)
        with open(path, encoding="utf-8") as f:
            content = f.read()
        assert "N/A" in content  # confirms N/A is rendered for missing data
        assert "H3" in content
