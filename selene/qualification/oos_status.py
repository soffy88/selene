"""Write oos-status.json from observed_live records only."""

from __future__ import annotations

import json
from pathlib import Path

from selene.evidence.schema import OOS_GATES
from selene.qualification.oos_report import build_gap_report

ROOT = Path(__file__).resolve().parents[2]


def write_oos_status(root: Path | None = None) -> Path:
    root = root or ROOT
    report = build_gap_report(root)
    n = int(report.get("n_trades") or 0)
    required = int(OOS_GATES["oos_closed_trades_min"])
    payload = {
        "generated_at": report["generated_at"],
        "observed_live_range": report.get("time_range") or {"start": None, "end": None},
        "n_trades": n,
        "required_trades": required,
        "missing_trades": max(0, required - n),
        "regimes": report.get("regimes") or [],
        "verdict": "BLOCKED_INSUFFICIENT_DATA",
        "n_backfill_excluded": report.get("n_backfill_excluded"),
        "I_HAVE_OOS_EVIDENCE": False,
        "historical_oi_source": "OWNER_BLOCKED",
    }
    out = root / "evidence" / "oos" / "oos-status.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out
