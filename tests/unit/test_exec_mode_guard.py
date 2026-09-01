"""Boot-guard tests for execution EXEC_MODE (P0-1).

Live production boot requires bound artifacts. I_HAVE_OOS_EVIDENCE cannot unlock
live. CONFIRM_THEN_EXEC is a deprecated alias of LIMITED_LIVE.
"""
from unittest.mock import patch

import pytest

import services.execution.main as m
from shared.runtime.release_identity import ExecMode, ExecModeError


def _run_guard(monkeypatch, exec_mode, env, **extra_env):
    monkeypatch.setattr(m, "EXEC_MODE", exec_mode)
    monkeypatch.setenv("ENVIRONMENT", env)
    for key in (
        "I_UNDERSTAND_LIVE_AUTO_EXEC",
        "I_HAVE_OOS_EVIDENCE",
        "SELENE_RELEASE_MANIFEST",
        "SELENE_OOS_ARTIFACT",
        "SELENE_SHADOW_ARTIFACT",
        "FUNDS_SCOPE",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in extra_env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    return m._assert_safe_exec_mode()


def test_unknown_mode_refuses(monkeypatch):
    with pytest.raises(ExecModeError, match="Unrecognized EXEC_MODE"):
        _run_guard(monkeypatch, "PAPPER", "development")


def test_notify_only_and_paper_production_allowed(monkeypatch):
    ident = _run_guard(monkeypatch, "NOTIFY_ONLY", "production")
    assert ident.exec_mode is ExecMode.NOTIFY_ONLY
    ident = _run_guard(monkeypatch, "PAPER", "production")
    assert ident.funds_scope == "paper"
    assert ident.adapters_enabled is False


def test_shadow_is_not_live(monkeypatch):
    ident = _run_guard(monkeypatch, "SHADOW", "production")
    assert ident.exec_mode is ExecMode.SHADOW
    assert ident.adapters_enabled is False
    assert ident.fill_ws_enabled is False
    assert ident.orderbook_rest_enabled is False


def test_confirm_then_exec_alias_is_limited_live(monkeypatch):
    ident = _run_guard(monkeypatch, "CONFIRM_THEN_EXEC", "development")
    assert ident.exec_mode is ExecMode.LIMITED_LIVE
    assert ident.funds_scope == "testnet"


def test_production_live_blocked_without_artifacts(monkeypatch):
    with pytest.raises(ExecModeError):
        _run_guard(monkeypatch, "CONFIRM_THEN_EXEC", "production", I_HAVE_OOS_EVIDENCE="yes")
    with pytest.raises(ExecModeError):
        _run_guard(
            monkeypatch,
            "AUTO_EXEC",
            "production",
            I_UNDERSTAND_LIVE_AUTO_EXEC="yes",
            I_HAVE_OOS_EVIDENCE="yes",
        )


def test_oos_env_var_is_not_qualification(monkeypatch):
    with pytest.raises(ExecModeError, match="release manifest|OOS artifact"):
        _run_guard(
            monkeypatch,
            "AUTO_EXEC",
            "production",
            I_HAVE_OOS_EVIDENCE="yes",
            I_UNDERSTAND_LIVE_AUTO_EXEC="yes",
        )


def test_non_production_live_is_testnet(monkeypatch):
    ident = _run_guard(monkeypatch, "AUTO_EXEC", "development")
    assert ident.funds_scope == "testnet"
    with pytest.raises(ExecModeError, match="FUNDS_SCOPE=mainnet is forbidden"):
        _run_guard(monkeypatch, "AUTO_EXEC", "development", FUNDS_SCOPE="mainnet")


def test_paper_init_skips_real_adapters(monkeypatch):
    monkeypatch.setattr(m, "EXEC_MODE", "PAPER")
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    monkeypatch.setenv("OKX_API_KEY", "k")
    monkeypatch.setenv("OKX_API_SECRET", "s")
    monkeypatch.setenv("OKX_PASSPHRASE", "p")
    with patch("services.execution.adapters.binance.BinanceAdapter") as binance, patch(
        "services.execution.adapters.okx.OKXAdapter"
    ) as okx:
        m._init_adapters()
        binance.assert_not_called()
        okx.assert_not_called()


def test_paper_process_signal_skips_orderbook_rest(monkeypatch):
    monkeypatch.setattr(m, "EXEC_MODE", "PAPER")
    assert m._live_venue_io_enabled() is False
    monkeypatch.setattr(m, "EXEC_MODE", "NOTIFY_ONLY")
    assert m._live_venue_io_enabled() is False
    monkeypatch.setattr(m, "EXEC_MODE", "SHADOW")
    assert m._live_venue_io_enabled() is False
    monkeypatch.setattr(m, "EXEC_MODE", "AUTO_EXEC")
    assert m._live_venue_io_enabled() is True
