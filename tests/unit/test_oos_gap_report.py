from __future__ import annotations

import json
from pathlib import Path

import pytest

from selene.evidence.schema import OOS_GATES
from selene.evidence.verify import ArtifactError, evaluate_oos_gates, verify_oos
from selene.qualification.oos_report import build_gap_report, write_gap_report


def test_oos_gap_report_stays_blocked_and_excludes_backfill(tmp_path: Path):
    oos = tmp_path / "evidence" / "oos"
    oos.mkdir(parents=True)
    (oos / "backfill.json").write_text(
        json.dumps(
            {
                "provenance": "backfilled",
                "metrics": {"oos_closed_trades": 500, "n_trades": 500},
                "regimes": ["RANGING", "TRENDING_UP", "HIGH_VOLATILITY"],
            }
        ),
        encoding="utf-8",
    )
    (oos / "live.json").write_text(
        json.dumps(
            {
                "provenance": "observed_live",
                "metrics": {"oos_closed_trades": 3, "n_trades": 3},
                "regimes": ["RANGING"],
                "oos_range": {"start": "2026-07-01", "end": "2026-07-02"},
            }
        ),
        encoding="utf-8",
    )
    report = build_gap_report(tmp_path)
    assert report["n_trades"] == 3
    assert report["n_backfill_excluded"] == 500
    assert report["status"] == "BLOCKED_INSUFFICIENT_DATA"
    assert report["I_HAVE_OOS_EVIDENCE"] is False
    assert OOS_GATES["oos_closed_trades_min"] == 100
    assert "BLOCKED_INSUFFICIENT_DATA:oos_closed_trades" in report["block_reasons"]
    assert report["time_range"]["start"] == "2026-07-01"
    reasons = evaluate_oos_gates(report["artifact"])
    assert any("oos_closed_trades" in r for r in reasons)
    path = write_gap_report(tmp_path)
    with pytest.raises(ArtifactError):
        verify_oos(str(path.parent / "does-not-exist.json"))
    assert json.loads(path.read_text())["gate_verdict"] == "BLOCKED"
