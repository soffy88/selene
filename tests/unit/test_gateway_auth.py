"""Gateway API-key auth tests (optimization item #21)."""
import asyncio

import pytest
from fastapi import HTTPException

import services.gateway.main as gw


def test_open_when_key_unset(monkeypatch):
    monkeypatch.setattr(gw, "GATEWAY_API_KEY", "")
    assert asyncio.run(gw.require_api_key(x_api_key="")) is True
    assert asyncio.run(gw.require_api_key(x_api_key="anything")) is True


def test_enforced_when_key_set(monkeypatch):
    monkeypatch.setattr(gw, "GATEWAY_API_KEY", "secret")
    assert asyncio.run(gw.require_api_key(x_api_key="secret")) is True
    with pytest.raises(HTTPException) as ei:
        asyncio.run(gw.require_api_key(x_api_key="wrong"))
    assert ei.value.status_code == 401
    with pytest.raises(HTTPException):
        asyncio.run(gw.require_api_key(x_api_key=""))


def test_single_circuit_breaker_reset_route():
    # The duplicate was removed (item #21); exactly one POST reset route remains.
    paths = [r for r in gw.app.routes
             if getattr(r, "path", "") == "/api/v4/risk/circuit-breaker/reset"
             and "POST" in getattr(r, "methods", set())]
    assert len(paths) == 1
