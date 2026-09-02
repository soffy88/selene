"""Execution-mode authority and production live boot verification (P0-1).

Missing EXEC_MODE defaults to PAPER. An unrecognized mode refuses to start.
Live modes never start in production without bound release/OOS/shadow artifacts.
I_HAVE_OOS_EVIDENCE is not a qualification signal and cannot unlock live.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Optional

_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")

LIVE_ENV_OOS_ACK = "I_HAVE_OOS_EVIDENCE"  # ignored; cannot fake qualification


class ExecMode(str, Enum):
    NOTIFY_ONLY = "NOTIFY_ONLY"
    PAPER = "PAPER"
    SHADOW = "SHADOW"
    LIMITED_LIVE = "LIMITED_LIVE"
    AUTO_EXEC = "AUTO_EXEC"


class ExecModeError(RuntimeError):
    """Boot-time execution identity failure. Process must exit; never degrade to live."""


_ALIASES = {
    "CONFIRM_THEN_EXEC": ExecMode.LIMITED_LIVE,
}

_PAPER_MODES = frozenset({ExecMode.NOTIFY_ONLY, ExecMode.PAPER, ExecMode.SHADOW})
_LIVE_MODES = frozenset({ExecMode.LIMITED_LIVE, ExecMode.AUTO_EXEC})

_OOS_REQUIRED = (
    "schema_version",
    "artifact_id",
    "generated_at",
    "expires_at",
    "strategy_commit",
    "image_digest",
    "strategy_config_digest",
    "risk_policy_digest",
    "data_manifest_digest",
    "gate_verdict",
    "artifact_digest",
)

_RELEASE_REQUIRED = (
    "release_id",
    "git_sha",
    "image_digests",
    "config_digest",
    "risk_policy_digest",
    "oos_artifact_id",
    "shadow_artifact_id",
)

_SHADOW_REQUIRED = (
    "schema_version",
    "artifact_id",
    "generated_at",
    "expires_at",
    "strategy_commit",
    "image_digest",
    "gate_verdict",
    "artifact_digest",
)


def parse_exec_mode(raw: Optional[str]) -> ExecMode:
    """Parse EXEC_MODE. Unset/empty → PAPER. Unknown → refuse (no silent fallback)."""
    if raw is None:
        return ExecMode.PAPER
    text = str(raw).strip()
    if text == "":
        return ExecMode.PAPER
    alias = _ALIASES.get(text)
    if alias is not None:
        return alias
    try:
        return ExecMode(text)
    except ValueError as exc:
        allowed = ", ".join(m.value for m in ExecMode)
        raise ExecModeError(
            f"Unrecognized EXEC_MODE={text!r}. Allowed: {allowed}. "
            f"Deprecated alias CONFIRM_THEN_EXEC maps to LIMITED_LIVE. "
            "Refusing to start."
        ) from exc


def is_live_mode(mode: ExecMode | str | None) -> bool:
    return parse_exec_mode(mode.value if isinstance(mode, ExecMode) else mode) in _LIVE_MODES


def is_paper_like(mode: ExecMode | str | None) -> bool:
    return parse_exec_mode(mode.value if isinstance(mode, ExecMode) else mode) in _PAPER_MODES


def should_init_exchange_adapters(mode: ExecMode | str | None) -> bool:
    """PAPER/NOTIFY_ONLY/SHADOW must not construct venue adapters."""
    return is_live_mode(mode)


def should_subscribe_fill_ws(mode: ExecMode | str | None) -> bool:
    return is_live_mode(mode)


def should_call_orderbook_rest(mode: ExecMode | str | None) -> bool:
    return is_live_mode(mode)


def funds_scope_for(mode: ExecMode, environment: str) -> str:
    if mode in _PAPER_MODES:
        return "paper"
    if (environment or "").strip().lower() == "production":
        return "mainnet"
    return "testnet"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: str) -> datetime:
    text = (value or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def canonical_json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def sha256_digest(payload: Any) -> str:
    body = dict(payload) if isinstance(payload, dict) else payload
    if isinstance(body, dict):
        body = {k: v for k, v in body.items() if k not in {"artifact_digest", "manifest_digest"}}
    return "sha256:" + hashlib.sha256(canonical_json_bytes(body)).hexdigest()


def _load_json_file(path: str, *, kind: str) -> dict[str, Any]:
    if not path or not str(path).strip():
        raise ExecModeError(f"Missing {kind} path; production live boot is fail-closed.")
    p = Path(path)
    if not p.is_file():
        raise ExecModeError(f"{kind} not found: {path}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExecModeError(f"Invalid {kind} at {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ExecModeError(f"{kind} must be a JSON object: {path}")
    return data


def _require_keys(data: Mapping[str, Any], keys: tuple[str, ...], *, kind: str) -> None:
    missing = [k for k in keys if k not in data or data[k] in (None, "")]
    if missing:
        raise ExecModeError(f"{kind} missing required fields: {', '.join(missing)}")


def _require_digest(value: str, *, field: str) -> None:
    if not _DIGEST.match((value or "").strip()):
        raise ExecModeError(f"{field} must be sha256:<64 hex>, got {value!r}")


def _require_sha(value: str, *, field: str) -> None:
    if not _SHA40.match((value or "").strip().lower()):
        raise ExecModeError(f"{field} must be a 40-char git SHA, got {value!r}")


def _require_pass_verdict(data: Mapping[str, Any], *, kind: str) -> None:
    verdict = str(data.get("gate_verdict") or "").upper()
    if verdict != "PASS":
        raise ExecModeError(f"{kind} gate_verdict={verdict!r} is not PASS; refusing live boot.")


def _require_not_expired(data: Mapping[str, Any], *, kind: str, now: datetime) -> None:
    expires = _parse_iso(str(data.get("expires_at") or ""))
    if expires <= now:
        raise ExecModeError(f"{kind} expired at {data.get('expires_at')}; refusing live boot.")


def _require_digest_match(data: Mapping[str, Any], *, kind: str) -> None:
    claimed = str(data.get("artifact_digest") or "")
    computed = sha256_digest(data)
    if claimed != computed:
        raise ExecModeError(f"{kind} artifact_digest mismatch: claimed={claimed} computed={computed}")


def resolve_git_sha(environ: Optional[Mapping[str, str]] = None) -> str:
    env = environ or os.environ
    for key in ("SELENE_GIT_SHA", "GIT_SHA", "SOURCE_VERSION"):
        value = (env.get(key) or "").strip()
        if value:
            return value
    return "unknown"


def resolve_image_digest(environ: Optional[Mapping[str, str]] = None) -> str:
    env = environ or os.environ
    for key in ("SELENE_IMAGE_DIGEST", "IMAGE_DIGEST"):
        value = (env.get(key) or "").strip()
        if value:
            return value
    return "unknown"


def resolve_config_digest(environ: Optional[Mapping[str, str]] = None) -> str:
    env = environ or os.environ
    return (env.get("SELENE_STRATEGY_CONFIG_DIGEST") or env.get("STRATEGY_CONFIG_DIGEST") or "").strip()


def resolve_risk_policy_digest(environ: Optional[Mapping[str, str]] = None) -> str:
    env = environ or os.environ
    return (env.get("SELENE_RISK_POLICY_DIGEST") or env.get("RISK_POLICY_DIGEST") or "").strip()


@dataclass(frozen=True)
class ReleaseIdentity:
    exec_mode: ExecMode
    exec_mode_raw: str
    environment: str
    funds_scope: str
    git_sha: str
    image_digest: str
    adapters_enabled: bool
    fill_ws_enabled: bool
    orderbook_rest_enabled: bool
    oos_artifact_id: Optional[str] = None
    shadow_artifact_id: Optional[str] = None
    release_id: Optional[str] = None
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def as_health(self) -> dict[str, Any]:
        return {
            "exec_mode": self.exec_mode.value,
            "exec_mode_raw": self.exec_mode_raw,
            "environment": self.environment,
            "funds_scope": self.funds_scope,
            "git_sha": self.git_sha,
            "image_digest": self.image_digest,
            "adapters_enabled": self.adapters_enabled,
            "fill_ws_enabled": self.fill_ws_enabled,
            "orderbook_rest_enabled": self.orderbook_rest_enabled,
            "oos_artifact_id": self.oos_artifact_id,
            "shadow_artifact_id": self.shadow_artifact_id,
            "release_id": self.release_id,
        }


def snapshot_identity(
    exec_mode: Optional[str],
    environment: str,
    *,
    environ: Optional[Mapping[str, str]] = None,
) -> ReleaseIdentity:
    """Non-verifying snapshot for paper/health. Still rejects unknown modes."""
    env = environ or os.environ
    raw = "" if exec_mode is None else str(exec_mode)
    mode = parse_exec_mode(exec_mode)
    claimed = (env.get("FUNDS_SCOPE") or env.get("SELENE_FUNDS_SCOPE") or "").strip().lower()
    scope = funds_scope_for(mode, environment)
    if claimed:
        if claimed == "mainnet" and scope != "mainnet":
            raise ExecModeError(
                f"FUNDS_SCOPE=mainnet is forbidden when computed scope is {scope} "
                f"(EXEC_MODE={mode.value} ENVIRONMENT={environment})."
            )
        if claimed not in {"paper", "testnet", "mainnet", "none"}:
            raise ExecModeError(f"Unrecognized FUNDS_SCOPE={claimed!r}.")
        if claimed == "mainnet" and (environment or "").strip().lower() != "production":
            raise ExecModeError("Non-production environment cannot claim funds_scope=mainnet.")
    return ReleaseIdentity(
        exec_mode=mode,
        exec_mode_raw=raw or mode.value,
        environment=environment or "development",
        funds_scope=scope,
        git_sha=resolve_git_sha(env),
        image_digest=resolve_image_digest(env),
        adapters_enabled=should_init_exchange_adapters(mode),
        fill_ws_enabled=should_subscribe_fill_ws(mode),
        orderbook_rest_enabled=should_call_orderbook_rest(mode),
    )


def verify_boot(
    exec_mode: Optional[str],
    environment: str,
    *,
    environ: Optional[Mapping[str, str]] = None,
    now: Optional[datetime] = None,
) -> ReleaseIdentity:
    """Fail-closed boot check. Live+production requires bound artifacts; never env-ack."""
    env = dict(environ or os.environ)
    identity = snapshot_identity(exec_mode, environment, environ=env)
    mode = identity.exec_mode
    env_name = (environment or "development").strip().lower()

    if env.get(LIVE_ENV_OOS_ACK, "").strip().lower() in {"1", "true", "yes", "on"}:
        if mode in _LIVE_MODES and env_name == "production":
            # Presence of the ack is not an error by itself, but it is never sufficient.
            pass

    if mode not in _LIVE_MODES:
        return identity

    if env_name != "production":
        if identity.funds_scope != "testnet":
            raise ExecModeError(f"Non-production live mode must use funds_scope=testnet, got {identity.funds_scope}.")
        return identity

    return _verify_production_live(identity, env, now=now or _utcnow())


def _verify_production_live(
    identity: ReleaseIdentity,
    env: Mapping[str, str],
    *,
    now: datetime,
) -> ReleaseIdentity:
    release_path = (env.get("SELENE_RELEASE_MANIFEST") or "").strip()
    oos_path = (env.get("SELENE_OOS_ARTIFACT") or "").strip()
    shadow_path = (env.get("SELENE_SHADOW_ARTIFACT") or "").strip()

    release = _load_json_file(release_path, kind="release manifest")
    oos = _load_json_file(oos_path, kind="OOS artifact")
    shadow = _load_json_file(shadow_path, kind="shadow artifact")

    _require_keys(release, _RELEASE_REQUIRED, kind="release manifest")
    _require_keys(oos, _OOS_REQUIRED, kind="OOS artifact")
    _require_keys(shadow, _SHADOW_REQUIRED, kind="shadow artifact")

    if str(oos.get("schema_version") or "") != "oos-artifact-v1":
        raise ExecModeError(f"Unknown OOS schema_version={oos.get('schema_version')!r}; refusing live boot.")
    if str(shadow.get("schema_version") or "") != "shadow-artifact-v1":
        raise ExecModeError(f"Unknown shadow schema_version={shadow.get('schema_version')!r}; refusing live boot.")

    _require_pass_verdict(oos, kind="OOS artifact")
    _require_pass_verdict(shadow, kind="shadow artifact")
    _require_not_expired(oos, kind="OOS artifact", now=now)
    _require_not_expired(shadow, kind="shadow artifact", now=now)
    _require_digest_match(oos, kind="OOS artifact")
    _require_digest_match(shadow, kind="shadow artifact")

    git_sha = identity.git_sha.strip().lower()
    _require_sha(git_sha, field="SELENE_GIT_SHA")
    _require_sha(str(release.get("git_sha") or ""), field="release.git_sha")
    _require_sha(str(oos.get("strategy_commit") or ""), field="oos.strategy_commit")
    _require_sha(str(shadow.get("strategy_commit") or ""), field="shadow.strategy_commit")

    if git_sha != str(release["git_sha"]).strip().lower():
        raise ExecModeError("release git_sha does not match running SELENE_GIT_SHA.")
    if git_sha != str(oos["strategy_commit"]).strip().lower():
        raise ExecModeError("OOS strategy_commit does not match running git SHA.")
    if git_sha != str(shadow["strategy_commit"]).strip().lower():
        raise ExecModeError("shadow strategy_commit does not match running git SHA.")

    image = identity.image_digest.strip()
    _require_digest(image, field="SELENE_IMAGE_DIGEST")
    image_digests = release.get("image_digests") or {}
    if not isinstance(image_digests, dict) or not image_digests:
        raise ExecModeError("release.image_digests must be a non-empty object.")
    if image not in {str(v) for v in image_digests.values()} and image != str(oos.get("image_digest") or ""):
        raise ExecModeError("running image digest is not listed in release.image_digests or OOS artifact.")
    if image != str(oos.get("image_digest") or ""):
        raise ExecModeError("OOS image_digest does not match running image digest.")
    if image != str(shadow.get("image_digest") or ""):
        raise ExecModeError("shadow image_digest does not match running image digest.")

    config_digest = resolve_config_digest(env)
    risk_digest = resolve_risk_policy_digest(env)
    if not config_digest:
        raise ExecModeError("SELENE_STRATEGY_CONFIG_DIGEST is required for production live boot.")
    if not risk_digest:
        raise ExecModeError("SELENE_RISK_POLICY_DIGEST is required for production live boot.")
    _require_digest(config_digest, field="SELENE_STRATEGY_CONFIG_DIGEST")
    _require_digest(risk_digest, field="SELENE_RISK_POLICY_DIGEST")
    if config_digest != str(oos.get("strategy_config_digest") or ""):
        raise ExecModeError("OOS strategy_config_digest does not match running config digest.")
    if config_digest != str(release.get("config_digest") or ""):
        raise ExecModeError("release config_digest does not match running config digest.")
    if risk_digest != str(oos.get("risk_policy_digest") or ""):
        raise ExecModeError("OOS risk_policy_digest does not match running risk policy digest.")
    if risk_digest != str(release.get("risk_policy_digest") or ""):
        raise ExecModeError("release risk_policy_digest does not match running risk policy digest.")

    if str(release.get("oos_artifact_id") or "") != str(oos.get("artifact_id") or ""):
        raise ExecModeError("release.oos_artifact_id does not match OOS artifact_id.")
    if str(release.get("shadow_artifact_id") or "") != str(shadow.get("artifact_id") or ""):
        raise ExecModeError("release.shadow_artifact_id does not match shadow artifact_id.")

    allowlist = (env.get("SELENE_VENUE_ALLOWLIST") or env.get("VENUE_ALLOWLIST") or "").strip()
    accounts = (env.get("SELENE_ACCOUNT_ALLOWLIST") or env.get("ACCOUNT_ALLOWLIST") or "").strip()
    capital = (env.get("SELENE_CAPITAL_CAP_USD") or env.get("CAPITAL_CAP_USD") or "").strip()
    if not allowlist:
        raise ExecModeError("SELENE_VENUE_ALLOWLIST is required for production live boot.")
    if not accounts:
        raise ExecModeError("SELENE_ACCOUNT_ALLOWLIST is required for production live boot.")
    try:
        cap = float(capital)
    except (TypeError, ValueError) as exc:
        raise ExecModeError("SELENE_CAPITAL_CAP_USD must be a positive number.") from exc
    if cap <= 0:
        raise ExecModeError("SELENE_CAPITAL_CAP_USD must be a positive number.")

    if identity.exec_mode is ExecMode.AUTO_EXEC:
        ack = (env.get("I_UNDERSTAND_LIVE_AUTO_EXEC") or "").strip().lower()
        if ack not in {"1", "true", "yes", "on"}:
            raise ExecModeError(
                "AUTO_EXEC in production also requires I_UNDERSTAND_LIVE_AUTO_EXEC=yes "
                "in addition to bound artifacts. Artifacts remain the authority; "
                "this ack is not a substitute for OOS evidence."
            )

    return ReleaseIdentity(
        exec_mode=identity.exec_mode,
        exec_mode_raw=identity.exec_mode_raw,
        environment=identity.environment,
        funds_scope="mainnet",
        git_sha=git_sha,
        image_digest=image,
        adapters_enabled=True,
        fill_ws_enabled=True,
        orderbook_rest_enabled=True,
        oos_artifact_id=str(oos.get("artifact_id")),
        shadow_artifact_id=str(shadow.get("artifact_id")),
        release_id=str(release.get("release_id")),
    )


def main(argv: Optional[list[str]] = None) -> int:
    """Process/container entry for fail-closed boot and health identity dump."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(prog="selene-release-identity")
    parser.add_argument(
        "--health",
        action="store_true",
        help="print execution identity health JSON (same fields as /health identity block)",
    )
    parser.parse_args(argv)
    try:
        identity = verify_boot(
            os.getenv("EXEC_MODE"),
            os.getenv("ENVIRONMENT", "development"),
        )
    except ExecModeError as exc:
        sys.stderr.write(json.dumps({"status": "fail", "error": str(exc)}) + "\n")
        return 2
    payload = identity.as_health()
    payload["status"] = "ok"
    sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
