#!/usr/bin/env python3
"""Fault injection against the isolated qualification stack (PAPER only)."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "evidence" / "smoke" / "qualification-faults.json"
COMPOSE = ["docker", "compose", "-f", str(ROOT / "docker-compose.qualification.yml")]


def _docker() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        subprocess.run(["docker", "info"], capture_output=True, timeout=10, check=True)
        return True
    except Exception:
        return False


def _compose(args: list[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run([*COMPOSE, *args], capture_output=True, text=True, cwd=ROOT, timeout=timeout)


def main() -> int:
    from selene.qualification.faults import run_faults

    with tempfile.TemporaryDirectory() as tmp:
        ledger = str(Path(tmp) / "ledger.sqlite")
        cases = [c.__dict__ for c in run_faults(ledger_path=ledger)]

    docker = _docker()
    infra: list[dict] = []
    if docker:
        for svc in ("qual-postgres", "qual-redis"):
            restarted = _compose(["restart", svc])
            infra.append({"name": f"restart_{svc}", "returncode": restarted.returncode})
        wait = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "wait_qualification.py"), "--timeout", "90"],
            cwd=ROOT,
        )
        infra.append({"name": "wait_after_restart", "returncode": wait.returncode})
        sql = "SELECT count(*) FROM signals WHERE id = '00000000-0000-4000-8000-000000000001';"
        probe = _compose(["exec", "-T", "qual-postgres", "psql", "-U", "selene", "-d", "selene", "-tAc", sql])
        redis = _compose(["exec", "-T", "qual-redis", "redis-cli", "ping"])
        infra.append(
            {
                "name": "postgres_recover",
                "returncode": probe.returncode,
                "stdout": (probe.stdout or "").strip(),
                "status": "PASS" if probe.returncode == 0 and probe.stdout.strip() == "1" else "FAIL",
            }
        )
        infra.append(
            {
                "name": "redis_recover",
                "returncode": redis.returncode,
                "stdout": (redis.stdout or "").strip(),
                "status": "PASS" if redis.returncode == 0 and redis.stdout.strip() == "PONG" else "FAIL",
            }
        )

    in_process_ok = all(c["status"] == "PASS" for c in cases)
    dup = next((c for c in cases if c["name"] == "duplicate_message"), None)
    duplicate_side_effects = 0 if dup and dup["detail"].get("submits") == 1 else None
    infra_ok = all(i.get("status") != "FAIL" and i.get("returncode", 0) == 0 for i in infra) if infra else False
    if not in_process_ok:
        status = "FAIL"
    elif docker and infra_ok:
        status = "PASS"
    elif docker:
        status = "PARTIAL"
    else:
        status = "RUNTIME_BLOCKED_NO_DOCKER"

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "docker": docker,
        "duplicate_external_side_effects": duplicate_side_effects,
        "cases": cases,
        "infra": infra,
        "exec_mode": "PAPER",
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": status, "out": str(OUT), "duplicate_side_effects": duplicate_side_effects}))
    return 2 if status == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
