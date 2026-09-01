from __future__ import annotations

import json
from pathlib import Path

import pytest

from selene.evidence.build import build_data_manifest, build_oos_artifact, build_shadow_artifact
from selene.evidence.verify import ArtifactError, verify_manifest, verify_oos, verify_shadow
from selene.qualification.verify_all import verify_all


def test_oos_fail_verdict_refused(tmp_path: Path):
    art = build_oos_artifact(gate_verdict="FAIL", fail_reasons=["no alpha"])
    path = tmp_path / "oos.json"
    path.write_text(json.dumps(art), encoding="utf-8")
    with pytest.raises(ArtifactError, match="gate_verdict"):
        verify_oos(str(path))


def test_oos_digest_mismatch_refused(tmp_path: Path):
    art = build_oos_artifact(gate_verdict="PASS")
    art["artifact_digest"] = "sha256:" + "ab" * 32
    path = tmp_path / "oos.json"
    path.write_text(json.dumps(art), encoding="utf-8")
    with pytest.raises(ArtifactError, match="mismatch"):
        verify_oos(str(path))


def test_insufficient_trades_blocked(tmp_path: Path):
    art = build_oos_artifact(
        gate_verdict="PASS",
        metrics={"oos_closed_trades": 3, "sharpe_after_cost": 2.0},
        cpcv={"median_sharpe": 0.5, "pbo": 0.1},
    )
    path = tmp_path / "oos.json"
    path.write_text(json.dumps(art), encoding="utf-8")
    with pytest.raises(ArtifactError, match="BLOCKED_INSUFFICIENT_DATA"):
        verify_oos(str(path))


def test_shadow_short_run_blocked(tmp_path: Path):
    art = build_shadow_artifact(gate_verdict="PASS", days_run=2, regimes_seen=1)
    path = tmp_path / "shadow.json"
    path.write_text(json.dumps(art), encoding="utf-8")
    with pytest.raises(ArtifactError, match="days_run"):
        verify_shadow(str(path))


def test_manifest_rejects_unknown_provenance(tmp_path: Path):
    man = build_data_manifest(provenance="observed_live")
    path = tmp_path / "man.json"
    path.write_text(json.dumps(man), encoding="utf-8")
    assert verify_manifest(str(path))["provenance"] == "observed_live"


def test_verify_all_fail_closed_without_paths():
    report = verify_all(release=None, oos=None, shadow=None, fail_closed=True)
    assert report["status"] == "NO_GO"
