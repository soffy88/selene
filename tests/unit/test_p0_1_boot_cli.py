"""Process-level P0-1 boot CLI (no monkeypatch of EXEC_MODE inside the SUT)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _run(env_extra: dict[str, str], args: list[str] | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    for key in (
        "I_HAVE_OOS_EVIDENCE",
        "I_UNDERSTAND_LIVE_AUTO_EXEC",
        "SELENE_RELEASE_MANIFEST",
        "SELENE_OOS_ARTIFACT",
        "SELENE_SHADOW_ARTIFACT",
        "FUNDS_SCOPE",
    ):
        env.pop(key, None)
    env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "shared.runtime.release_identity", *(args or [])],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_cli_paper_health_json():
    proc = _run({"EXEC_MODE": "PAPER", "ENVIRONMENT": "production"}, ["--health"])
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["status"] == "ok"
    assert payload["exec_mode"] == "PAPER"
    assert payload["funds_scope"] == "paper"
    assert payload["adapters_enabled"] is False


def test_cli_unknown_mode_exits_nonzero():
    proc = _run({"EXEC_MODE": "PAPPER", "ENVIRONMENT": "production"})
    assert proc.returncode == 2
    err = json.loads(proc.stderr.strip().splitlines()[-1])
    assert err["status"] == "fail"
    assert "Unrecognized EXEC_MODE" in err["error"]


def test_cli_production_live_without_artifacts_exits():
    proc = _run(
        {
            "EXEC_MODE": "AUTO_EXEC",
            "ENVIRONMENT": "production",
            "I_HAVE_OOS_EVIDENCE": "yes",
            "I_UNDERSTAND_LIVE_AUTO_EXEC": "yes",
        }
    )
    assert proc.returncode == 2
    err = json.loads(proc.stderr.strip().splitlines()[-1])
    assert "release manifest" in err["error"]
