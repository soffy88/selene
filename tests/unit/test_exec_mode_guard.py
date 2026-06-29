"""Boot-guard tests for execution EXEC_MODE (optimization item #7).

CONFIRM_THEN_EXEC also reaches mainnet (after a manual confirm), so it must be
gated like AUTO_EXEC when ENVIRONMENT=production without an explicit ack.
"""
import importlib

import pytest

import services.execution.main as m


def _run_guard(monkeypatch, exec_mode, env, ack=None):
    monkeypatch.setattr(m, "EXEC_MODE", exec_mode)
    monkeypatch.setenv("ENVIRONMENT", env)
    if ack is None:
        monkeypatch.delenv("I_UNDERSTAND_LIVE_AUTO_EXEC", raising=False)
    else:
        monkeypatch.setenv("I_UNDERSTAND_LIVE_AUTO_EXEC", ack)
    m._assert_safe_exec_mode()


def test_confirm_then_exec_production_blocked(monkeypatch):
    with pytest.raises(RuntimeError):
        _run_guard(monkeypatch, "CONFIRM_THEN_EXEC", "production")


def test_auto_exec_production_blocked(monkeypatch):
    with pytest.raises(RuntimeError):
        _run_guard(monkeypatch, "AUTO_EXEC", "production")


def test_ack_overrides(monkeypatch):
    _run_guard(monkeypatch, "CONFIRM_THEN_EXEC", "production", ack="yes")  # no raise
    _run_guard(monkeypatch, "AUTO_EXEC", "production", ack="yes")          # no raise


def test_notify_only_production_allowed(monkeypatch):
    _run_guard(monkeypatch, "NOTIFY_ONLY", "production")  # no raise


def test_paper_production_allowed(monkeypatch):
    _run_guard(monkeypatch, "PAPER", "production")  # no raise


def test_live_mode_development_allowed(monkeypatch):
    _run_guard(monkeypatch, "CONFIRM_THEN_EXEC", "development")  # no raise
    _run_guard(monkeypatch, "AUTO_EXEC", "development")          # no raise
