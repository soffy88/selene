"""Honest shadow gap report. Days/regimes come from records, never the calendar."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from selene.evidence.build import build_shadow_artifact
from selene.evidence.schema import SHADOW_GATES
from selene.evidence.verify import evaluate_shadow_gates

ROOT = Path(__file__).resolve().parents[2]
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


def collect_shadow_records(root: Path | None = None) -> dict[str, Any]:
    root = root or ROOT
    shadow_dir = root / "evidence" / "shadow"
    days: set[str] = set()
    regimes: set[str] = set()
    event_count = 0
    reconcile_diff = 0
    stale_actions = 0
    ledger_gaps = 0
    latencies: list[float] = []
    simulated_fills = 0
    slippage: list[float] = []
    sources: list[str] = []
    touches_live_api = False

    if shadow_dir.is_dir():
        for path in sorted(shadow_dir.glob("*.json")):
            if path.name in SKIP_NAMES:
                continue
            data = _load_json(path)
            if data is None:
                continue
            sources.append(path.name)
            for day in data.get("days") or []:
                days.add(str(day)[:10])
            if data.get("day"):
                days.add(str(data["day"])[:10])
            for r in data.get("regimes") or []:
                regimes.add(str(r))
            event_count += int(data.get("event_count") or data.get("events") or 0)
            reconcile_diff += int(data.get("unresolved_reconcile_diff") or data.get("reconciliation_diff") or 0)
            stale_actions += int(data.get("stale_data_actions") or data.get("stale_action") or 0)
            ledger_gaps += int(data.get("ledger_gaps") or data.get("ledger_gap") or 0)
            if data.get("latency_ms") is not None:
                latencies.append(float(data["latency_ms"]))
            simulated_fills += int(data.get("simulated_fills") or 0)
            if data.get("slippage_drift") is not None:
                slippage.append(float(data["slippage_drift"]))
            if data.get("touched_live_api") is True:
                touches_live_api = True
            # days_run in a file is only used if it is itself a list of dated records
            for rec in data.get("records") or []:
                if rec.get("day"):
                    days.add(str(rec["day"])[:10])
                if rec.get("regime"):
                    regimes.add(str(rec["regime"]))
                event_count += 1

    return {
        "days_run": len(days),
        "days": sorted(days),
        "regimes_seen": len(regimes),
        "regimes": sorted(regimes),
        "event_count": event_count,
        "unresolved_reconcile_diff": reconcile_diff,
        "stale_data_actions": stale_actions,
        "ledger_gaps": ledger_gaps,
        "latency_ms": (sum(latencies) / len(latencies)) if latencies else None,
        "simulated_fills": simulated_fills,
        "slippage_drift": (sum(slippage) / len(slippage)) if slippage else None,
        "touched_live_api": touches_live_api,
        "sources": sources,
        "calendar_not_used": True,
    }


def build_gap_report(root: Path | None = None) -> dict[str, Any]:
    observed = collect_shadow_records(root)
    fail_reasons: list[str] = []
    if observed["days_run"] < SHADOW_GATES["days_run_min"]:
        fail_reasons.append("BLOCKED_INSUFFICIENT_DATA:days_run")
    if observed["regimes_seen"] < SHADOW_GATES["regimes_min"]:
        fail_reasons.append("BLOCKED_INSUFFICIENT_DATA:regimes")
    if observed["touched_live_api"]:
        fail_reasons.append("SHADOW_TOUCHED_LIVE_API")
    artifact = build_shadow_artifact(
        strategy_commit=_git_sha(),
        gate_verdict="BLOCKED",
        days_run=observed["days_run"],
        regimes_seen=observed["regimes_seen"],
        unexplained_halts=0,
        duplicate_intents=0,
        unresolved_reconcile_diff=observed["unresolved_reconcile_diff"],
        stale_data_actions=observed["stale_data_actions"],
        ledger_gaps=observed["ledger_gaps"],
        fail_reasons=fail_reasons,
        event_count=observed["event_count"],
        latency_ms=observed["latency_ms"],
        simulated_fills=observed["simulated_fills"],
        slippage_drift=observed["slippage_drift"],
        shadow_only=True,
    )
    payload = {
        "generated_at": _iso(datetime.now(timezone.utc)),
        "status": "BLOCKED",
        "gate_verdict": "BLOCKED",
        "days_run": observed["days_run"],
        "days_run_min": SHADOW_GATES["days_run_min"],
        "regimes_seen": observed["regimes_seen"],
        "regimes_min": SHADOW_GATES["regimes_min"],
        "regimes": observed["regimes"],
        "event_count": observed["event_count"],
        "unresolved_reconcile_diff": observed["unresolved_reconcile_diff"],
        "stale_data_actions": observed["stale_data_actions"],
        "ledger_gaps": observed["ledger_gaps"],
        "latency_ms": observed["latency_ms"],
        "simulated_fills": observed["simulated_fills"],
        "slippage_drift": observed["slippage_drift"],
        "touched_live_api": observed["touched_live_api"],
        "calendar_not_used": True,
        "block_reasons": fail_reasons,
        "evaluate_shadow_gates": evaluate_shadow_gates(artifact),
        "sources": observed["sources"],
        "artifact": artifact,
    }
    return payload


def write_gap_report(root: Path | None = None) -> Path:
    root = root or ROOT
    payload = build_gap_report(root)
    out = root / "evidence" / "shadow" / "gap-report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out
