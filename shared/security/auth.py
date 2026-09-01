"""Gateway RBAC. Production refuses to start without secrets. Writes never anonymous."""

from __future__ import annotations

import hmac
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Optional

from fastapi import Header, HTTPException, Request

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
FORBIDDEN_QUERY_KEYS = frozenset({"api_key", "apikey", "x-api-key", "token", "secret"})
MAX_SKEW_SECONDS = 300
DEFAULT_WRITE_RATE_PER_MIN = 30


class Role(str, Enum):
    READ = "read"
    OPERATOR = "operator"
    ADMIN = "admin"


_RANK = {Role.READ: 1, Role.OPERATOR: 2, Role.ADMIN: 3}


class GatewayAuthError(RuntimeError):
    """Boot-time auth configuration failure."""


class IdempotentReplay(Exception):
    """Raised when a write request_id already completed; handler returns the stored response."""

    def __init__(self, stored: dict[str, Any]):
        super().__init__("idempotent replay")
        self.stored = stored


@dataclass(frozen=True)
class Principal:
    role: Role
    key_id: str


@dataclass(frozen=True)
class WriteContext:
    principal: Principal
    request_id: str
    actor: str
    timestamp: str
    reason: str
    path: str
    method: str


def _env(environ: Optional[Mapping[str, str]] = None) -> Mapping[str, str]:
    return environ or os.environ


def configured_secrets(environ: Optional[Mapping[str, str]] = None) -> dict[Role, str]:
    env = _env(environ)
    legacy = (env.get("GATEWAY_API_KEY") or "").strip()
    return {
        Role.READ: (env.get("GATEWAY_READ_KEY") or legacy).strip(),
        Role.OPERATOR: (env.get("GATEWAY_OPERATOR_KEY") or legacy).strip(),
        Role.ADMIN: (env.get("GATEWAY_ADMIN_KEY") or legacy).strip(),
    }


def assert_gateway_auth_ready(
    environment: str,
    environ: Optional[Mapping[str, str]] = None,
) -> None:
    env_name = (environment or "development").strip().lower()
    if env_name != "production":
        return
    secrets = configured_secrets(environ)
    missing = [role.value for role, secret in secrets.items() if not secret]
    if missing:
        raise GatewayAuthError(
            "ENVIRONMENT=production refuses to start without gateway auth secrets "
            f"for roles: {', '.join(missing)}. Set GATEWAY_READ_KEY, "
            "GATEWAY_OPERATOR_KEY, GATEWAY_ADMIN_KEY (or GATEWAY_API_KEY as a shared secret)."
        )


def _match_role(api_key: str, secrets: Mapping[Role, str]) -> Optional[Role]:
    if not api_key:
        return None
    presented = api_key.encode("utf-8")
    matched: Optional[Role] = None
    # Highest privilege first so a shared legacy key authenticates as admin.
    for role in (Role.ADMIN, Role.OPERATOR, Role.READ):
        secret = secrets.get(role) or ""
        if not secret:
            continue
        if hmac.compare_digest(presented, secret.encode("utf-8")):
            matched = role
            break
    return matched


def authenticate(
    api_key: str,
    required: Role,
    *,
    environment: str,
    environ: Optional[Mapping[str, str]] = None,
) -> Principal:
    secrets = configured_secrets(environ)
    env_name = (environment or "development").strip().lower()
    role = _match_role(api_key, secrets)
    if role is None:
        if env_name != "production" and required is Role.READ and not any(secrets.values()):
            return Principal(role=Role.READ, key_id="anonymous-dev-read")
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")
    if _RANK[role] < _RANK[required]:
        raise HTTPException(status_code=403, detail=f"role {role.value} cannot perform {required.value} actions")
    return Principal(role=role, key_id=f"{role.value}-key")


def reject_query_secrets(request: Request) -> None:
    for key in request.query_params.keys():
        if key.lower() in FORBIDDEN_QUERY_KEYS:
            raise HTTPException(status_code=400, detail="credentials must not be passed in the query string")


def parse_timestamp(raw: str) -> datetime:
    text = (raw or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="X-Timestamp is required for write requests")
    try:
        if text.isdigit():
            ts = datetime.fromtimestamp(int(text), tz=timezone.utc)
        else:
            iso = text[:-1] + "+00:00" if text.endswith("Z") else text
            ts = datetime.fromisoformat(iso)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=400, detail="X-Timestamp must be unix seconds or ISO-8601") from exc
    now = datetime.now(timezone.utc)
    skew = abs((now - ts.astimezone(timezone.utc)).total_seconds())
    if skew > MAX_SKEW_SECONDS:
        raise HTTPException(status_code=401, detail="request timestamp outside allowed clock skew")
    return ts


_RATE: dict[str, list[float]] = {}


def check_rate_limit(key_id: str, *, now: Optional[float] = None, limit: int = DEFAULT_WRITE_RATE_PER_MIN) -> None:
    ts = now if now is not None else time.time()
    window = _RATE.setdefault(key_id, [])
    cutoff = ts - 60.0
    window[:] = [item for item in window if item >= cutoff]
    if len(window) >= limit:
        raise HTTPException(status_code=429, detail="write rate limit exceeded")
    window.append(ts)


def require_role(required: Role):
    """FastAPI dependency factory. Write routes also require request_id/actor/timestamp/reason."""

    async def dependency(
        request: Request,
        x_api_key: str = Header(default="", alias="X-API-Key"),
        x_request_id: str = Header(default="", alias="X-Request-Id"),
        x_actor: str = Header(default="", alias="X-Actor"),
        x_timestamp: str = Header(default="", alias="X-Timestamp"),
        x_reason: str = Header(default="", alias="X-Reason"),
    ) -> WriteContext | Principal:
        reject_query_secrets(request)
        environment = os.getenv("ENVIRONMENT", "development")
        principal = authenticate(x_api_key, required, environment=environment)
        if request.method not in WRITE_METHODS:
            return principal
        if not (x_request_id or "").strip():
            raise HTTPException(status_code=400, detail="X-Request-Id is required for write requests")
        if not (x_actor or "").strip():
            raise HTTPException(status_code=400, detail="X-Actor is required for write requests")
        if not (x_reason or "").strip():
            raise HTTPException(status_code=400, detail="X-Reason is required for write requests")
        parse_timestamp(x_timestamp)
        check_rate_limit(principal.key_id)
        from shared.security.audit import get_ledger

        replay = get_ledger().lookup(x_request_id.strip())
        if replay is not None:
            raise IdempotentReplay(replay)
        ctx = WriteContext(
            principal=principal,
            request_id=x_request_id.strip(),
            actor=x_actor.strip(),
            timestamp=x_timestamp.strip(),
            reason=x_reason.strip(),
            path=request.url.path,
            method=request.method,
        )
        request.state.write_context = ctx
        return ctx

    return dependency


def maybe_replay(request: Request) -> Optional[dict[str, Any]]:
    return getattr(request.state, "idempotent_replay", None)
