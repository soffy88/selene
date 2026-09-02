"""Honest OOS gap report. Never fabricates PASS or observed_live from backfill."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from selene.evidence.build import build_oos_artifact
from selene.evidence.schema import OOS_GATES
from selene.evidence.verify import evaluate_oos_gates

ROOT = Path(__file__).resolve().parents[2]
OOS_DIR = ROOT / "evidence" / "oos"
SKIP_NAMES = {"gap-report.json", "current.json"}


def _git_sha() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return "0" * 40


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def collect_observed_live(root: Path | None = None) -> dict[str, Any]:
    """Count only records marked observed_live. Backfill/replay are reported as gaps."""
    root = root or ROOT
    oos_dir = root / "evidence" / "oos"
    n_trades = 0
    n_backfill = 0
    n_replay = 0
    regimes: set[str] = set()
    starts: list[str] = []
    ends: list[str] = []
    sources: list[str] = []

    if oos_dir.is_dir():
        for path in sorted(oos_dir.glob("*.json")):
            if path.name in SKIP_NAMES:
                continue
            data = _load_json(path)
            if data is None:
                continue
            provenance = str(data.get("provenance") or data.get("metrics", {}).get("provenance") or "")
            metrics = data.get("metrics") or {}
            trades = int(metrics.get("oos_closed_trades") or metrics.get("n_trades") or data.get("n_trades") or 0)
            if provenance == "backfilled":
                n_backfill += trades
                sources.append(path.name)
                continue
            if provenance in {"derived_replay", "backtest", "synthetic"}:
                n_replay += trades
                sources.append(path.name)
                continue
            if provenance != "observed_live":
                # Unlabeled historical files are not observed_live.
                n_replay += trades
                sources.append(path.name)
                continue
            n_trades += trades
            for r in data.get("regimes") or metrics.get("regimes") or []:
                regimes.add(str(r))
            rng = data.get("oos_range") or {}
            if rng.get("start"):
                starts.append(str(rng["start"]))
            if rng.get("end"):
                ends.append(str(rng["end"]))
            sources.append(path.name)

    return {
        "n_trades": n_trades,
        "n_backfill_excluded": n_backfill,
        "n_replay_excluded": n_replay,
        "regimes": sorted(regimes),
        "time_range": {"start": min(starts) if starts else None, "end": max(ends) if ends else None},
        "sources": sources,
        "observed_live_floor": "2026-06-15",
    }


def build_gap_report(root: Path | None = None) -> dict[str, Any]:
    observed = collect_observed_live(root)
    fail_reasons = []
    if observed["n_trades"] < OOS_GATES["oos_closed_trades_min"]:
        fail_reasons.append("BLOCKED_INSUFFICIENT_DATA:oos_closed_trades")
    if len(observed["regimes"]) < OOS_GATES["oos_active_states_min"]:
        fail_reasons.append("BLOCKED_INSUFFICIENT_DATA:oos_active_states")
    artifact = build_oos_artifact(
        strategy_commit=_git_sha(),
        gate_verdict="BLOCKED",
        fail_reasons=fail_reasons,
        metrics={
            "oos_closed_trades": observed["n_trades"],
            "n_trades": observed["n_trades"],
            "oos_active_states": len(observed["regimes"]),
            "sharpe_after_cost": 0.0,
            "deflated_sharpe_prob": 0.0,
            "bootstrap_sharpe_p5": -1.0,
            "max_drawdown": 1.0,
            "catastrophic_loss": 0,
            "lookahead_findings": 0,
            "provenance": "observed_live",
        },
        cpcv={},
        oos_range=observed["time_range"],
        instruments=["BTCUSDT"],
        producer={"job": "oos-gap-report", "i_have_oos_evidence": False},
    )
    gate_reasons = evaluate_oos_gates(artifact)
    payload = {
        "generated_at": _iso(datetime.now(timezone.utc)),
        "status": "BLOCKED_INSUFFICIENT_DATA",
        "gate_verdict": "BLOCKED",
        "n_trades": observed["n_trades"],
        "oos_closed_trades_min": OOS_GATES["oos_closed_trades_min"],
        "regimes": observed["regimes"],
        "time_range": observed["time_range"],
        "block_reasons": fail_reasons,
        "evaluate_oos_gates": gate_reasons,
        "n_backfill_excluded": observed["n_backfill_excluded"],
        "n_replay_excluded": observed["n_replay_excluded"],
        "sources": observed["sources"],
        "I_HAVE_OOS_EVIDENCE": False,
        "artifact": artifact,
    }
    return payload


def write_gap_report(root: Path | None = None) -> Path:
    root = root or ROOT
    payload = build_gap_report(root)
    out = root / "evidence" / "oos" / "gap-report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out
