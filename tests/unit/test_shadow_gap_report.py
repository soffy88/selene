from __future__ import annotations

import json
from pathlib import Path

from selene.evidence.schema import SHADOW_GATES
from selene.qualification.shadow_report import build_gap_report, write_gap_report


def test_shadow_gap_report_uses_records_not_calendar(tmp_path: Path):
    shadow = tmp_path / "evidence" / "shadow"
    shadow.mkdir(parents=True)
    (shadow / "day1.json").write_text(
        json.dumps(
            {
                "day": "2026-07-01",
                "regimes": ["RANGING"],
                "event_count": 4,
                "unresolved_reconcile_diff": 0,
                "stale_data_actions": 1,
                "ledger_gaps": 0,
                "latency_ms": 12.0,
                "simulated_fills": 2,
                "slippage_drift": 0.1,
                "touched_live_api": False,
            }
        ),
        encoding="utf-8",
    )
    report = build_gap_report(tmp_path)
    assert report["days_run"] == 1
    assert report["regimes_seen"] == 1
    assert report["event_count"] == 4
    assert report["stale_data_actions"] == 1
    assert report["simulated_fills"] == 2
    assert report["calendar_not_used"] is True
    assert report["touched_live_api"] is False
    assert SHADOW_GATES["days_run_min"] == 30
    assert SHADOW_GATES["regimes_min"] == 3
    assert "BLOCKED_INSUFFICIENT_DATA:days_run" in report["block_reasons"]
    assert "BLOCKED_INSUFFICIENT_DATA:regimes" in report["block_reasons"]
    path = write_gap_report(tmp_path)
    assert json.loads(path.read_text())["gate_verdict"] == "BLOCKED"


def test_shadow_empty_records_are_zero_days(tmp_path: Path):
    (tmp_path / "evidence" / "shadow").mkdir(parents=True)
    report = build_gap_report(tmp_path)
    assert report["days_run"] == 0
    assert report["regimes_seen"] == 0
    assert report["status"] == "BLOCKED"
