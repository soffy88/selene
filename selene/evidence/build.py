"""Build canonical artifacts and bind digests."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from selene.evidence.schema import (
    MANIFEST_SCHEMA_VERSION,
    OOS_SCHEMA_VERSION,
    RELEASE_SCHEMA_VERSION,
    SHADOW_SCHEMA_VERSION,
)
from shared.runtime.release_identity import sha256_digest


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def bind(payload: dict[str, Any], digest_field: str = "artifact_digest") -> dict[str, Any]:
    body = dict(payload)
    body.pop(digest_field, None)
    body.pop("manifest_digest", None)
    digest = sha256_digest(body)
    if digest_field == "manifest_digest":
        body["manifest_digest"] = digest
    else:
        body["artifact_digest"] = digest
    return body


def build_oos_artifact(**fields: Any) -> dict[str, Any]:
    now = _now()
    payload = {
        "schema_version": OOS_SCHEMA_VERSION,
        "artifact_id": str(uuid.uuid4()),
        "generated_at": _iso(now),
        "expires_at": _iso(now + timedelta(days=30)),
        "strategy_commit": fields.get("strategy_commit", "0" * 40),
        "image_digest": fields.get("image_digest", "sha256:" + "00" * 32),
        "strategy_config_digest": fields.get("strategy_config_digest", "sha256:" + "11" * 32),
        "risk_policy_digest": fields.get("risk_policy_digest", "sha256:" + "22" * 32),
        "feature_schema_version": fields.get("feature_schema_version", "sel-v2"),
        "data_manifest_digest": fields.get("data_manifest_digest", "sha256:" + "33" * 32),
        "train_range": fields.get("train_range", {}),
        "embargo_range": fields.get("embargo_range", {}),
        "oos_range": fields.get("oos_range", {}),
        "instruments": fields.get("instruments", ["BTC-USDT"]),
        "venues": fields.get("venues", ["binance"]),
        "cost_model": fields.get("cost_model", {"fee_bps": 4, "slippage_bps": 1}),
        "search_trials": int(fields.get("search_trials", 0)),
        "metrics": fields.get("metrics", {}),
        "cpcv": fields.get("cpcv", {}),
        "gate_verdict": fields.get("gate_verdict", "FAIL"),
        "fail_reasons": list(fields.get("fail_reasons", [])),
        "producer": fields.get("producer", {"job": "qualification"}),
    }
    payload.update({k: v for k, v in fields.items() if k not in payload})
    return bind(payload)


def build_shadow_artifact(**fields: Any) -> dict[str, Any]:
    now = _now()
    payload = {
        "schema_version": SHADOW_SCHEMA_VERSION,
        "artifact_id": str(uuid.uuid4()),
        "generated_at": _iso(now),
        "expires_at": _iso(now + timedelta(days=30)),
        "strategy_commit": fields.get("strategy_commit", "0" * 40),
        "image_digest": fields.get("image_digest", "sha256:" + "00" * 32),
        "gate_verdict": fields.get("gate_verdict", "FAIL"),
        "days_run": int(fields.get("days_run", 0)),
        "regimes_seen": int(fields.get("regimes_seen", 0)),
        "unexplained_halts": int(fields.get("unexplained_halts", 0)),
        "duplicate_intents": int(fields.get("duplicate_intents", 0)),
        "unresolved_reconcile_diff": int(fields.get("unresolved_reconcile_diff", 0)),
        "stale_data_actions": int(fields.get("stale_data_actions", 0)),
        "ledger_gaps": int(fields.get("ledger_gaps", 0)),
        "fail_reasons": list(fields.get("fail_reasons", [])),
    }
    payload.update({k: v for k, v in fields.items() if k not in payload})
    return bind(payload)


def build_data_manifest(**fields: Any) -> dict[str, Any]:
    now = _now()
    payload = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "manifest_id": str(uuid.uuid4()),
        "generated_at": _iso(now),
        "source": fields.get("source", "unknown"),
        "license": fields.get("license", "OWNER_BLOCKED"),
        "fetched_at": fields.get("fetched_at", _iso(now)),
        "object_digest": fields.get("object_digest", "sha256:" + "44" * 32),
        "symbols": fields.get("symbols", ["BTC-USDT"]),
        "venues": fields.get("venues", ["binance"]),
        "time_range": fields.get("time_range", {}),
        "bar_count": int(fields.get("bar_count", 0)),
        "tick_count": int(fields.get("tick_count", 0)),
        "missing_rate": float(fields.get("missing_rate", 0)),
        "duplicate_rate": float(fields.get("duplicate_rate", 0)),
        "out_of_order_rate": float(fields.get("out_of_order_rate", 0)),
        "provenance": fields.get("provenance", "observed_live"),
        "timezone": fields.get("timezone", "UTC"),
        "feature_available_at": fields.get("feature_available_at", "bar_close"),
        "corporate_contract_changes": fields.get("corporate_contract_changes", []),
    }
    payload.update({k: v for k, v in fields.items() if k not in payload})
    return bind(payload, digest_field="manifest_digest")


def build_release_manifest(**fields: Any) -> dict[str, Any]:
    now = _now()
    payload = {
        "schema_version": RELEASE_SCHEMA_VERSION,
        "release_id": fields.get("release_id", str(__import__("uuid").uuid4())),
        "git_sha": fields.get("git_sha", "0" * 40),
        "image_digests": fields.get("image_digests", {"execution": "sha256:" + "00" * 32}),
        "config_digest": fields.get("config_digest", "sha256:" + "11" * 32),
        "db_schema_version": fields.get("db_schema_version", "ledger-v1"),
        "risk_policy_digest": fields.get("risk_policy_digest", "sha256:" + "22" * 32),
        "oos_artifact_id": fields.get("oos_artifact_id", ""),
        "shadow_artifact_id": fields.get("shadow_artifact_id", ""),
        "required_ci_run": fields.get("required_ci_run", "not-run"),
        "created_at": _iso(now),
    }
    payload.update({k: v for k, v in fields.items() if k not in payload})
    return payload


def dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, indent=2) + "\n"
