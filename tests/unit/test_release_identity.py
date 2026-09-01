"""P0-1 release identity and execution-mode fail-closed tests."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from shared.runtime.release_identity import (
    ExecMode,
    ExecModeError,
    funds_scope_for,
    parse_exec_mode,
    sha256_digest,
    should_call_orderbook_rest,
    should_init_exchange_adapters,
    should_subscribe_fill_ws,
    snapshot_identity,
    verify_boot,
)

SHA = "24137634f777e7d993434faafc2d418e31f7b7b1"
IMAGE = "sha256:" + ("ab" * 32)
CONFIG = "sha256:" + ("cd" * 32)
RISK = "sha256:" + ("ef" * 32)
NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def _future() -> str:
    return (NOW + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _past() -> str:
    return (NOW - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _oos_payload(**overrides):
    body = {
        "schema_version": "oos-artifact-v1",
        "artifact_id": "oos-1",
        "generated_at": "2026-08-01T00:00:00Z",
        "expires_at": _future(),
        "strategy_commit": SHA,
        "image_digest": IMAGE,
        "strategy_config_digest": CONFIG,
        "risk_policy_digest": RISK,
        "feature_schema_version": "v1",
        "data_manifest_digest": "sha256:" + ("11" * 32),
        "gate_verdict": "PASS",
        "fail_reasons": [],
    }
    body.update(overrides)
    body["artifact_digest"] = sha256_digest(body)
    return body


def _shadow_payload(**overrides):
    body = {
        "schema_version": "shadow-artifact-v1",
        "artifact_id": "shadow-1",
        "generated_at": "2026-08-01T00:00:00Z",
        "expires_at": _future(),
        "strategy_commit": SHA,
        "image_digest": IMAGE,
        "gate_verdict": "PASS",
        "fail_reasons": [],
    }
    body.update(overrides)
    body["artifact_digest"] = sha256_digest(body)
    return body


def _release_payload(**overrides):
    body = {
        "release_id": "rel-1",
        "git_sha": SHA,
        "image_digests": {"execution": IMAGE},
        "config_digest": CONFIG,
        "db_schema_version": "1",
        "risk_policy_digest": RISK,
        "oos_artifact_id": "oos-1",
        "shadow_artifact_id": "shadow-1",
        "required_ci_run": "ci-1",
        "created_at": "2026-08-01T00:00:00Z",
    }
    body.update(overrides)
    return body


def _write_artifacts(tmp_path: Path, *, oos=None, shadow=None, release=None):
    oos_path = tmp_path / "oos.json"
    shadow_path = tmp_path / "shadow.json"
    release_path = tmp_path / "release.json"
    oos_path.write_text(json.dumps(oos or _oos_payload()), encoding="utf-8")
    shadow_path.write_text(json.dumps(shadow or _shadow_payload()), encoding="utf-8")
    release_path.write_text(json.dumps(release or _release_payload()), encoding="utf-8")
    return {
        "SELENE_RELEASE_MANIFEST": str(release_path),
        "SELENE_OOS_ARTIFACT": str(oos_path),
        "SELENE_SHADOW_ARTIFACT": str(shadow_path),
        "SELENE_GIT_SHA": SHA,
        "SELENE_IMAGE_DIGEST": IMAGE,
        "SELENE_STRATEGY_CONFIG_DIGEST": CONFIG,
        "SELENE_RISK_POLICY_DIGEST": RISK,
        "SELENE_VENUE_ALLOWLIST": "binance",
        "SELENE_ACCOUNT_ALLOWLIST": "sub-1",
        "SELENE_CAPITAL_CAP_USD": "1000",
        "I_UNDERSTAND_LIVE_AUTO_EXEC": "yes",
    }


def test_parse_missing_and_empty_default_to_paper():
    assert parse_exec_mode(None) is ExecMode.PAPER
    assert parse_exec_mode("") is ExecMode.PAPER
    assert parse_exec_mode("   ") is ExecMode.PAPER


def test_unrecognized_mode_refuses_start():
    with pytest.raises(ExecModeError, match="Unrecognized EXEC_MODE='PAPPER'"):
        parse_exec_mode("PAPPER")
    with pytest.raises(ExecModeError, match="Unrecognized EXEC_MODE='LIVE'"):
        snapshot_identity("LIVE", "development")


def test_confirm_then_exec_maps_to_limited_live():
    assert parse_exec_mode("CONFIRM_THEN_EXEC") is ExecMode.LIMITED_LIVE


def test_paper_like_disables_venue_side_effects():
    for mode in ("NOTIFY_ONLY", "PAPER", "SHADOW", None, ""):
        parsed = parse_exec_mode(mode)
        assert should_init_exchange_adapters(parsed) is False
        assert should_subscribe_fill_ws(parsed) is False
        assert should_call_orderbook_rest(parsed) is False
        assert funds_scope_for(parsed, "production") == "paper"


def test_live_modes_enable_venue_side_effects():
    for mode in (ExecMode.LIMITED_LIVE, ExecMode.AUTO_EXEC):
        assert should_init_exchange_adapters(mode) is True
        assert should_subscribe_fill_ws(mode) is True
        assert should_call_orderbook_rest(mode) is True


def test_non_production_live_is_testnet_not_mainnet():
    ident = verify_boot("AUTO_EXEC", "development")
    assert ident.funds_scope == "testnet"
    ident = verify_boot("LIMITED_LIVE", "staging")
    assert ident.funds_scope == "testnet"


def test_testnet_environment_cannot_claim_mainnet():
    with pytest.raises(ExecModeError, match="FUNDS_SCOPE=mainnet is forbidden"):
        snapshot_identity(
            "AUTO_EXEC",
            "development",
            environ={"FUNDS_SCOPE": "mainnet"},
        )


def test_paper_production_does_not_require_artifacts():
    ident = verify_boot("PAPER", "production")
    assert ident.exec_mode is ExecMode.PAPER
    assert ident.funds_scope == "paper"
    assert ident.adapters_enabled is False


def test_production_live_without_artifacts_refuses():
    with pytest.raises(ExecModeError, match="Missing release manifest"):
        verify_boot("AUTO_EXEC", "production", environ={"I_HAVE_OOS_EVIDENCE": "yes"})


def test_oos_env_ack_cannot_unlock_live(tmp_path):
    env = {"I_HAVE_OOS_EVIDENCE": "yes", "I_UNDERSTAND_LIVE_AUTO_EXEC": "yes"}
    with pytest.raises(ExecModeError, match="release manifest"):
        verify_boot("AUTO_EXEC", "production", environ=env)


def test_production_live_with_bound_artifacts_passes(tmp_path):
    env = _write_artifacts(tmp_path)
    ident = verify_boot("LIMITED_LIVE", "production", environ=env, now=NOW)
    assert ident.funds_scope == "mainnet"
    assert ident.oos_artifact_id == "oos-1"
    assert ident.release_id == "rel-1"


def test_artifact_sha_mismatch_refuses(tmp_path):
    env = _write_artifacts(tmp_path)
    env["SELENE_GIT_SHA"] = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    with pytest.raises(ExecModeError, match="git_sha"):
        verify_boot("LIMITED_LIVE", "production", environ=env, now=NOW)


def test_artifact_config_hash_mismatch_refuses(tmp_path):
    env = _write_artifacts(tmp_path)
    env["SELENE_STRATEGY_CONFIG_DIGEST"] = "sha256:" + ("99" * 32)
    with pytest.raises(ExecModeError, match="strategy_config_digest"):
        verify_boot("LIMITED_LIVE", "production", environ=env, now=NOW)


def test_expired_artifact_refuses(tmp_path):
    oos = _oos_payload(expires_at=_past())
    env = _write_artifacts(tmp_path, oos=oos)
    with pytest.raises(ExecModeError, match="expired"):
        verify_boot("LIMITED_LIVE", "production", environ=env, now=NOW)


def test_fail_verdict_refuses(tmp_path):
    oos = _oos_payload(gate_verdict="FAIL")
    env = _write_artifacts(tmp_path, oos=oos)
    with pytest.raises(ExecModeError, match="gate_verdict"):
        verify_boot("LIMITED_LIVE", "production", environ=env, now=NOW)


def test_unknown_schema_refuses(tmp_path):
    oos = _oos_payload(schema_version="oos-artifact-v0")
    env = _write_artifacts(tmp_path, oos=oos)
    with pytest.raises(ExecModeError, match="Unknown OOS schema_version"):
        verify_boot("LIMITED_LIVE", "production", environ=env, now=NOW)
