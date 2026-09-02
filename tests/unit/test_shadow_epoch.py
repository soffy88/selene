from __future__ import annotations

import json
from pathlib import Path

from selene.qualification.shadow_epoch import open_or_continue_epoch, write_status


def test_shadow_epoch_resumes_same_digest(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "evidence" / "shadow").mkdir(parents=True)
    monkeypatch.setattr("selene.qualification.shadow_epoch.ROOT", tmp_path)
    first = open_or_continue_epoch(tmp_path)
    out = tmp_path / "evidence" / "shadow" / "epoch.json"
    out.write_text(json.dumps(first), encoding="utf-8")
    second = open_or_continue_epoch(tmp_path)
    assert second["epoch_id"] == first["epoch_id"]
    assert second["status"] == "open"
    path = write_status(tmp_path)
    payload = json.loads(path.read_text())
    assert payload["status"] == "BLOCKED"
    assert payload["days_run"] == 0
    assert payload["gate_verdict"] != "PASS"


def test_shadow_epoch_rotates_on_config_change(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("selene.qualification.shadow_epoch.ROOT", tmp_path)
    (tmp_path / "evidence" / "shadow").mkdir(parents=True)
    epoch = open_or_continue_epoch(tmp_path)
    epoch["config_digest"] = "sha256:" + "ab" * 32
    (tmp_path / "evidence" / "shadow" / "epoch.json").write_text(json.dumps(epoch), encoding="utf-8")
    nxt = open_or_continue_epoch(tmp_path)
    assert nxt["epoch_id"] != epoch["epoch_id"]
