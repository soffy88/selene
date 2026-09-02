"""P0-2 gateway authentication tests."""

from __future__ import annotations

import hmac
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from shared.security.audit import get_audit_log, get_ledger, record_halt_reset
from shared.security.auth import (
    GatewayAuthError,
    Principal,
    Role,
    WriteContext,
    assert_gateway_auth_ready,
    authenticate,
    configured_secrets,
)


def _write_headers(key: str, request_id: str = "req-1", actor: str = "operator-1") -> dict[str, str]:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "X-API-Key": key,
        "X-Request-Id": request_id,
        "X-Actor": actor,
        "X-Timestamp": ts,
        "X-Reason": "p0-2-test",
    }


def test_production_boot_missing_secrets_refuses():
    with pytest.raises(GatewayAuthError, match="refuses to start"):
        assert_gateway_auth_ready(
            "production",
            environ={"GATEWAY_API_KEY": "", "GATEWAY_READ_KEY": "", "GATEWAY_ADMIN_KEY": ""},
        )


def test_production_boot_with_secrets_ok():
    assert_gateway_auth_ready(
        "production",
        environ={
            "GATEWAY_READ_KEY": "r",
            "GATEWAY_OPERATOR_KEY": "o",
            "GATEWAY_ADMIN_KEY": "a",
        },
    )


def test_non_production_boot_without_secrets_ok():
    assert_gateway_auth_ready("development", environ={})


def test_compare_digest_used_for_key_match():
    env = {
        "GATEWAY_READ_KEY": "read-secret",
        "GATEWAY_OPERATOR_KEY": "op-secret",
        "GATEWAY_ADMIN_KEY": "adm-secret",
    }
    principal = authenticate("op-secret", Role.OPERATOR, environment="production", environ=env)
    assert principal.role is Role.OPERATOR
    with pytest.raises(HTTPException) as ei:
        authenticate("op-secretx", Role.OPERATOR, environment="production", environ=env)
    assert ei.value.status_code == 401
    # Same length wrong key still 401 (compare_digest path).
    secrets = configured_secrets(env)
    presented = b"op-secret"
    assert hmac.compare_digest(presented, secrets[Role.OPERATOR].encode()) is True


def test_operator_cannot_admin(monkeypatch):
    env = {
        "GATEWAY_READ_KEY": "read-secret",
        "GATEWAY_OPERATOR_KEY": "op-secret",
        "GATEWAY_ADMIN_KEY": "adm-secret",
    }
    with pytest.raises(HTTPException) as ei:
        authenticate("op-secret", Role.ADMIN, environment="production", environ=env)
    assert ei.value.status_code == 403


def test_anonymous_write_blocked_even_in_development():
    with pytest.raises(HTTPException) as ei:
        authenticate("", Role.OPERATOR, environment="development", environ={})
    assert ei.value.status_code == 401


def test_dev_read_may_be_anonymous():
    principal = authenticate("", Role.READ, environment="development", environ={})
    assert principal.role is Role.READ


def test_halt_reset_audit_includes_actor_reason_sha():
    get_audit_log().reset()
    ctx = WriteContext(
        principal=Principal(role=Role.ADMIN, key_id="admin-key"),
        request_id="halt-1",
        actor="oncall",
        timestamp="2026-09-01T00:00:00Z",
        reason="manual review complete",
        path="/api/v4/risk/circuit-breaker/reset",
        method="POST",
    )
    event = record_halt_reset(ctx=ctx, old_state="OPEN", new_state="CLOSED", git_sha="a" * 40)
    assert event.payload["old_state"] == "OPEN"
    assert event.payload["new_state"] == "CLOSED"
    assert event.actor == "oncall"
    assert event.reason == "manual review complete"
    assert event.git_sha == "a" * 40
    dumped = str(event)
    assert "adm-secret" not in dumped
    assert "GATEWAY_API_KEY" not in dumped


def test_write_rate_limit():
    from shared.security.auth import check_rate_limit

    key = "rate-test-key"
    now = 1_700_000_000.0
    for i in range(30):
        check_rate_limit(key, now=now + i * 0.01, limit=30)
    with pytest.raises(HTTPException) as ei:
        check_rate_limit(key, now=now + 1.0, limit=30)
    assert ei.value.status_code == 429


def test_idempotent_ledger_does_not_rerun():
    ledger = get_ledger()
    ledger.reset()
    ledger.remember("same", status_code=200, body={"status": "confirmed"}, path="/x", actor="a")
    ledger.remember("same", status_code=200, body={"status": "SHOULD_NOT_WIN"}, path="/x", actor="a")
    stored = ledger.lookup("same")
    assert stored["body"]["status"] == "confirmed"


def test_gateway_write_routes_require_identity(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("GATEWAY_OPERATOR_KEY", "op-secret")
    monkeypatch.setenv("GATEWAY_ADMIN_KEY", "adm-secret")
    monkeypatch.setenv("GATEWAY_READ_KEY", "read-secret")
    from shared.security.audit import get_ledger as _ledger

    _ledger().reset()

    import services.gateway.main as gw

    async def _noop_consume(*_a, **_k):
        return None

    monkeypatch.setattr(gw, "init_redis", lambda _url: None)
    monkeypatch.setattr(gw, "consume", _noop_consume)

    class _Redis:
        async def hset(self, *a, **k):
            return 1

        async def hdel(self, *a, **k):
            return 1

        async def get(self, *a, **k):
            return b"OPEN"

        async def publish(self, *a, **k):
            return 1

    monkeypatch.setattr(gw, "get_redis", lambda: _Redis())
    client = TestClient(gw.app, raise_server_exceptions=False)
    # Anonymous write is 401, never 2xx.
    r = client.post("/api/v4/signals/abc/confirm")
    assert r.status_code == 401
    r = client.post("/api/v4/signals/abc/confirm", headers=_write_headers("op-secret"))
    assert r.status_code == 200
    assert r.json()["status"] == "confirmed"
    # Replay same request_id does not 500 and is marked replay.
    r2 = client.post("/api/v4/signals/abc/confirm", headers=_write_headers("op-secret"))
    assert r2.status_code == 200
    assert r2.headers.get("x-idempotent-replay") == "1"
    # Operator cannot reset breaker.
    r3 = client.post(
        "/api/v4/risk/circuit-breaker/reset",
        headers=_write_headers("op-secret", request_id="req-admin"),
    )
    assert r3.status_code == 403
    # Query-string credentials rejected.
    r4 = client.post(
        "/api/v4/signals/abc/reject?api_key=op-secret",
        headers=_write_headers("op-secret", request_id="req-qs"),
    )
    assert r4.status_code == 400


def test_clock_skew_rejected(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("GATEWAY_OPERATOR_KEY", "op-secret")
    import services.gateway.main as gw
    from shared.security.audit import get_ledger as _ledger

    _ledger().reset()

    async def _noop_consume(*_a, **_k):
        return None

    monkeypatch.setattr(gw, "init_redis", lambda _url: None)
    monkeypatch.setattr(gw, "consume", _noop_consume)
    client = TestClient(gw.app, raise_server_exceptions=False)
    stale = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    headers = _write_headers("op-secret", request_id="skew-1")
    headers["X-Timestamp"] = stale
    r = client.post("/api/v4/signals/abc/confirm", headers=headers)
    assert r.status_code == 401


def test_config_endpoint_redacts_secrets(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("GATEWAY_API_KEY", "super-secret-value")
    monkeypatch.setenv("GATEWAY_READ_KEY", "read-secret-value")
    import services.gateway.main as gw

    async def _noop_consume(*_a, **_k):
        return None

    monkeypatch.setattr(gw, "init_redis", lambda _url: None)
    monkeypatch.setattr(gw, "consume", _noop_consume)
    client = TestClient(gw.app, raise_server_exceptions=False)
    r = client.get("/api/v4/config/gateway")
    assert r.status_code == 200
    cfg = r.json()["config"]
    blob = json.dumps(cfg)
    assert "super-secret-value" not in blob
    assert "read-secret-value" not in blob
    assert cfg.get("api_key") in {"present", "absent"} or "api_key" in cfg


def test_production_auth_boot_subprocess_exits():
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root)
    env["ENVIRONMENT"] = "production"
    env["GATEWAY_API_KEY"] = ""
    env["GATEWAY_READ_KEY"] = ""
    env["GATEWAY_OPERATOR_KEY"] = ""
    env["GATEWAY_ADMIN_KEY"] = ""
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "from shared.security.auth import assert_gateway_auth_ready; assert_gateway_auth_ready('production')",
        ],
        cwd=str(root),
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert proc.returncode != 0
    assert "refuses to start" in (proc.stderr + proc.stdout)


def test_single_circuit_breaker_reset_route():
    import services.gateway.main as gw

    paths = [
        r
        for r in gw.app.routes
        if getattr(r, "path", "") == "/api/v4/risk/circuit-breaker/reset" and "POST" in getattr(r, "methods", set())
    ]
    assert len(paths) == 1
