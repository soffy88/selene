#!/usr/bin/env python3
"""P0-1 process/container fail-closed probe.

Runs isolated interpreter processes (and a Docker python image when available)
so EXEC_MODE negatives and PAPER health identity are proven outside pytest
monkeypatch. Writes evidence/smoke/p0-1-runtime.json.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "evidence" / "smoke" / "p0-1-runtime.json"
PYTHON = sys.executable
MODULE = "shared.runtime.release_identity"


def _run(env: dict[str, str], extra_args: list[str] | None = None) -> dict:
    cmd = [PYTHON, "-m", MODULE]
    if extra_args:
        cmd.extend(extra_args)
    proc = subprocess.run(
        cmd,
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    parsed = None
    for blob in (stdout, stderr):
        if not blob:
            continue
        try:
            parsed = json.loads(blob.splitlines()[-1])
            break
        except json.JSONDecodeError:
            continue
    return {
        "returncode": proc.returncode,
        "stdout": stdout[-2000:],
        "stderr": stderr[-2000:],
        "parsed": parsed,
    }


def _base_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    env.pop("I_HAVE_OOS_EVIDENCE", None)
    env.pop("I_UNDERSTAND_LIVE_AUTO_EXEC", None)
    env.pop("SELENE_RELEASE_MANIFEST", None)
    env.pop("SELENE_OOS_ARTIFACT", None)
    env.pop("SELENE_SHADOW_ARTIFACT", None)
    env.pop("FUNDS_SCOPE", None)
    return env


def _expect_fail(name: str, env: dict[str, str], match: str) -> dict:
    result = _run(env)
    text = f"{result.get('stderr','')} {result.get('stdout','')}"
    ok = result["returncode"] != 0 and match.lower() in text.lower()
    return {"name": name, "status": "PASS" if ok else "FAIL", "result": result, "match": match}


def _expect_health(name: str, env: dict[str, str], expect: dict[str, object]) -> dict:
    result = _run(env, extra_args=["--health"])
    parsed = result.get("parsed") or {}
    ok = result["returncode"] == 0 and all(parsed.get(k) == v for k, v in expect.items())
    return {"name": name, "status": "PASS" if ok else "FAIL", "result": result, "expect": expect}


def _docker_negatives() -> dict:
    docker = shutil.which("docker")
    if not docker:
        return {"name": "docker_unknown_mode", "status": "BLOCKED", "detail": "docker binary not found"}
    cmd = [
        docker,
        "run",
        "--rm",
        "-e",
        "PYTHONPATH=/app",
        "-e",
        "EXEC_MODE=PAPPER",
        "-e",
        "ENVIRONMENT=production",
        "-v",
        f"{ROOT}:/app:ro",
        "-w",
        "/app",
        "python:3.12-slim",
        "python",
        "-m",
        MODULE,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired as exc:
        return {"name": "docker_unknown_mode", "status": "FAIL", "detail": f"timeout: {exc}"}
    except OSError as exc:
        return {"name": "docker_unknown_mode", "status": "BLOCKED", "detail": str(exc)}
    text = f"{proc.stdout} {proc.stderr}"
    ok = proc.returncode != 0 and "unrecognized exec_mode" in text.lower()
    return {
        "name": "docker_unknown_mode",
        "status": "PASS" if ok else ("BLOCKED" if proc.returncode in (125, 127) or "Unable to find" in text or "pull access" in text.lower() else "FAIL"),
        "returncode": proc.returncode,
        "stderr": (proc.stderr or "")[-1500:],
        "stdout": (proc.stdout or "")[-1500:],
    }


def main() -> int:
    cases = []
    env = _base_env()

    paper = env.copy()
    paper["EXEC_MODE"] = "PAPER"
    paper["ENVIRONMENT"] = "production"
    cases.append(
        _expect_health(
            "paper_production_health",
            paper,
            {
                "status": "ok",
                "exec_mode": "PAPER",
                "funds_scope": "paper",
                "adapters_enabled": False,
                "fill_ws_enabled": False,
                "orderbook_rest_enabled": False,
            },
        )
    )

    unknown = env.copy()
    unknown["EXEC_MODE"] = "PAPPER"
    unknown["ENVIRONMENT"] = "production"
    cases.append(_expect_fail("unknown_mode_exits", unknown, "Unrecognized EXEC_MODE"))

    live = env.copy()
    live["EXEC_MODE"] = "AUTO_EXEC"
    live["ENVIRONMENT"] = "production"
    live["I_HAVE_OOS_EVIDENCE"] = "yes"
    live["I_UNDERSTAND_LIVE_AUTO_EXEC"] = "yes"
    cases.append(_expect_fail("production_live_no_artifact_exits", live, "release manifest"))

    claim = env.copy()
    claim["EXEC_MODE"] = "AUTO_EXEC"
    claim["ENVIRONMENT"] = "development"
    claim["FUNDS_SCOPE"] = "mainnet"
    cases.append(_expect_fail("testnet_cannot_claim_mainnet", claim, "FUNDS_SCOPE=mainnet"))

    docker_case = _docker_negatives()
    cases.append(docker_case)

    failed = [c for c in cases if c.get("status") == "FAIL"]
    blocked = [c for c in cases if c.get("status") == "BLOCKED"]
    payload = {
        "probe": "p0-1-runtime",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "python": PYTHON,
        "root": str(ROOT),
        "status": "FAIL" if failed else ("PARTIAL" if blocked else "PASS"),
        "cases": cases,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"status": payload["status"], "out": str(OUT), "fail": len(failed), "blocked": len(blocked)}))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
