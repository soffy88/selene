"""Verify artifacts. FAIL/unknown schema/digest mismatch/expiry refuse."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from selene.evidence.schema import (
    MANIFEST_REQUIRED,
    OOS_GATES,
    OOS_REQUIRED,
    OOS_SCHEMA_VERSION,
    RELEASE_REQUIRED,
    SHADOW_GATES,
    SHADOW_REQUIRED,
    SHADOW_SCHEMA_VERSION,
)
from shared.runtime.release_identity import ExecModeError, sha256_digest


class ArtifactError(RuntimeError):
    pass


def _load(path: str) -> dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ArtifactError(f"{path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ArtifactError(f"{path} is not a JSON object")
    return data


def _require(data: dict[str, Any], keys: tuple[str, ...], kind: str) -> None:
    missing = [k for k in keys if k not in data or data[k] in (None, "")]
    if missing:
        raise ArtifactError(f"{kind} missing {missing}")


def _not_expired(data: dict[str, Any], kind: str) -> None:
    raw = str(data.get("expires_at") or "")
    if not raw:
        raise ArtifactError(f"{kind} missing expires_at")
    text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    expires = datetime.fromisoformat(text)
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires <= datetime.now(timezone.utc):
        raise ArtifactError(f"{kind} expired at {raw}")


def _digest_ok(data: dict[str, Any], field: str, kind: str) -> None:
    claimed = str(data.get(field) or "")
    computed = sha256_digest(data)
    if claimed != computed:
        raise ArtifactError(f"{kind} {field} mismatch claimed={claimed} computed={computed}")


def evaluate_oos_gates(data: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    metrics = data.get("metrics") or {}
    cpcv = data.get("cpcv") or {}
    trades = int(metrics.get("oos_closed_trades") or 0)
    if trades < OOS_GATES["oos_closed_trades_min"]:
        reasons.append("BLOCKED_INSUFFICIENT_DATA:oos_closed_trades")
    if float(metrics.get("sharpe_after_cost") or 0) <= OOS_GATES["sharpe_after_cost_min"]:
        reasons.append("sharpe_after_cost")
    if float(metrics.get("deflated_sharpe_prob") or 0) < OOS_GATES["deflated_sharpe_prob_min"]:
        reasons.append("deflated_sharpe_prob")
    if float(metrics.get("bootstrap_sharpe_p5") or -1) <= OOS_GATES["bootstrap_sharpe_p5_min"]:
        reasons.append("bootstrap_sharpe_p5")
    if float(metrics.get("max_drawdown") or 1) > OOS_GATES["max_drawdown_max"]:
        reasons.append("max_drawdown")
    if cpcv.get("median_sharpe") is None:
        reasons.append("cpcv_missing")
    elif float(cpcv.get("median_sharpe") or 0) <= OOS_GATES["cpcv_median_sharpe_min"]:
        reasons.append("cpcv_median_sharpe")
    if float(cpcv.get("pbo") or 1) >= OOS_GATES["cpcv_pbo_max"]:
        reasons.append("cpcv_pbo")
    if int(metrics.get("catastrophic_loss") or 0) > 0:
        reasons.append("catastrophic_loss")
    if int(metrics.get("lookahead_findings") or 0) > 0:
        reasons.append("lookahead")
    if int(metrics.get("oos_active_states") or 0) < OOS_GATES["oos_active_states_min"]:
        reasons.append("oos_active_states")
    return reasons


def evaluate_shadow_gates(data: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if int(data.get("days_run") or 0) < SHADOW_GATES["days_run_min"]:
        reasons.append("BLOCKED_INSUFFICIENT_DATA:days_run")
    if int(data.get("regimes_seen") or 0) < SHADOW_GATES["regimes_min"]:
        reasons.append("BLOCKED_INSUFFICIENT_DATA:regimes")
    for field, cap in (
        ("unexplained_halts", "unexplained_halts_max"),
        ("duplicate_intents", "duplicate_intents_max"),
        ("unresolved_reconcile_diff", "unresolved_reconcile_diff_max"),
        ("stale_data_actions", "stale_data_actions_max"),
        ("ledger_gaps", "ledger_gaps_max"),
    ):
        if int(data.get(field) or 0) > SHADOW_GATES[cap]:
            reasons.append(field)
    return reasons


def verify_oos(path: str) -> dict[str, Any]:
    data = _load(path)
    if data.get("schema_version") != OOS_SCHEMA_VERSION:
        raise ArtifactError(f"unknown OOS schema {data.get('schema_version')}")
    _require(data, OOS_REQUIRED, "OOS")
    _not_expired(data, "OOS")
    _digest_ok(data, "artifact_digest", "OOS")
    if data.get("gate_verdict") != "PASS":
        raise ArtifactError(f"OOS gate_verdict={data.get('gate_verdict')}")
    reasons = evaluate_oos_gates(data)
    if reasons:
        raise ArtifactError("OOS gates failed: " + ",".join(reasons))
    return data


def verify_shadow(path: str) -> dict[str, Any]:
    data = _load(path)
    if data.get("schema_version") != SHADOW_SCHEMA_VERSION:
        raise ArtifactError(f"unknown shadow schema {data.get('schema_version')}")
    _require(data, SHADOW_REQUIRED, "shadow")
    _not_expired(data, "shadow")
    _digest_ok(data, "artifact_digest", "shadow")
    if data.get("gate_verdict") != "PASS":
        raise ArtifactError(f"shadow gate_verdict={data.get('gate_verdict')}")
    reasons = evaluate_shadow_gates(data)
    if reasons:
        raise ArtifactError("shadow gates failed: " + ",".join(reasons))
    return data


def verify_release(path: str) -> dict[str, Any]:
    data = _load(path)
    _require(data, RELEASE_REQUIRED, "release")
    return data


def verify_manifest(path: str) -> dict[str, Any]:
    data = _load(path)
    _require(data, MANIFEST_REQUIRED, "data-manifest")
    _digest_ok(data, "manifest_digest", "data-manifest")
    provenance = str(data.get("provenance") or "")
    if provenance not in {"observed_live", "backfilled", "derived_replay"}:
        raise ArtifactError(f"invalid provenance {provenance}")
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path")
    parser.add_argument("--kind", default="oos", choices=["oos", "shadow", "release", "manifest"])
    args = parser.parse_args(argv)
    fn = {
        "oos": verify_oos,
        "shadow": verify_shadow,
        "release": verify_release,
        "manifest": verify_manifest,
    }[args.kind]
    try:
        fn(args.path)
    except (ArtifactError, ExecModeError, OSError, json.JSONDecodeError) as exc:
        sys.stderr.write(json.dumps({"status": "fail", "error": str(exc)}) + "\n")
        return 2
    sys.stdout.write(json.dumps({"status": "ok", "path": args.path, "kind": args.kind}) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
