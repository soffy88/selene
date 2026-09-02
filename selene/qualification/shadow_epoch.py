"""Immutable shadow epoch recorder. Shadow-only: never calls a venue API."""

from __future__ import annotations

import hashlib
import json
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
EPOCH_PATH = ROOT / "evidence" / "shadow" / "epoch.json"
STATUS_PATH = ROOT / "evidence" / "shadow" / "shadow-status.json"


def _iso(dt: datetime | None = None) -> str:
    return (dt or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _git_sha() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return "0" * 40


def config_digest(root: Path | None = None) -> str:
    root = root or ROOT
    payload = {
        "exec_mode": "PAPER",
        "shadow_only": True,
        "cost_model": {"fee_bps": 4, "slippage_bps": 1},
        "strategy": "sel-v2",
        "files": [],
    }
    for rel in (
        "services/execution/main.py",
        "services/signal/main.py",
        "selene/evidence/schema.py",
    ):
        path = root / rel
        if path.is_file():
            payload["files"].append({"path": rel, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return "sha256:" + digest


def _load(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _days_from_records(epoch: dict[str, Any]) -> int:
    days = {str(d)[:10] for d in epoch.get("days") or [] if d}
    return len(days)


def open_or_continue_epoch(root: Path | None = None) -> dict[str, Any]:
    root = root or ROOT
    sha = _git_sha()
    digest = config_digest(root)
    current = _load(root / "evidence" / "shadow" / "epoch.json")
    if current and current.get("status") == "open":
        same = current.get("starting_sha") == sha and current.get("config_digest") == digest
        if same:
            current["days_run"] = _days_from_records(current)
            current["updated_at"] = _iso()
            return current
        current["status"] = "closed"
        current["closed_at"] = _iso()
        current["closed_reason"] = "code_or_config_changed"
        archive = root / "evidence" / "shadow" / f"epoch-{current.get('epoch_id')}.json"
        archive.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    epoch = {
        "epoch_id": str(uuid.uuid4()),
        "starting_sha": sha,
        "config_digest": digest,
        "started_at": _iso(),
        "updated_at": _iso(),
        "observed_live_start": None,
        "days_run": 0,
        "days": [],
        "regimes": [],
        "event_count": 0,
        "reconciliation_diff": 0,
        "stale_actions": 0,
        "ledger_gaps": 0,
        "duplicate_intents": 0,
        "status": "open",
        "shadow_only": True,
        "touched_live_api": False,
        "gate_verdict": "BLOCKED",
    }
    return epoch


def write_status(root: Path | None = None) -> Path:
    root = root or ROOT
    epoch = open_or_continue_epoch(root)
    out_epoch = root / "evidence" / "shadow" / "epoch.json"
    out_status = root / "evidence" / "shadow" / "shadow-status.json"
    out_epoch.parent.mkdir(parents=True, exist_ok=True)
    out_epoch.write_text(json.dumps(epoch, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    status = {
        "generated_at": _iso(),
        "status": "BLOCKED",
        "gate_verdict": "BLOCKED",
        "epoch_id": epoch["epoch_id"],
        "starting_sha": epoch["starting_sha"],
        "config_digest": epoch["config_digest"],
        "started_at": epoch["started_at"],
        "observed_live_start": epoch["observed_live_start"],
        "days_run": epoch["days_run"],
        "days_run_min": 30,
        "regimes": epoch["regimes"],
        "regimes_min": 3,
        "event_count": epoch["event_count"],
        "reconciliation_diff": epoch["reconciliation_diff"],
        "stale_actions": epoch["stale_actions"],
        "ledger_gaps": epoch["ledger_gaps"],
        "duplicate_intents": epoch["duplicate_intents"],
        "touched_live_api": False,
        "calendar_not_used": True,
        "block_reasons": [
            "BLOCKED_INSUFFICIENT_DATA:days_run",
            "BLOCKED_INSUFFICIENT_DATA:regimes",
        ],
        "note": "Starting the recorder is not a 30-day gate PASS.",
    }
    out_status.write_text(json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out_status
