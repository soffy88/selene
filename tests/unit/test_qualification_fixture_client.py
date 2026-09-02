from __future__ import annotations

from services.scanner.fixture_client import FixtureScanClient
from services.scanner.main import _build_client


def test_scanner_source_fixture_uses_fixture_client(monkeypatch):
    monkeypatch.setenv("SCANNER_SOURCE", "fixture")
    client = _build_client(session=None)
    assert isinstance(client, FixtureScanClient)
