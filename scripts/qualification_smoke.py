#!/usr/bin/env python3
"""PAPER chain smoke against isolated qualification postgres/redis.

Never claims full scanner..gateway compose PASS: this compose is postgres+redis only.
The PAPER chain itself is in-process. Docker absent → RUNTIME_BLOCKED_NO_DOCKER.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "evidence" / "smoke" / "qualification-stack.json"
COMPOSE = ["docker", "compose", "-f", str(ROOT / "docker-compose.qualification.yml")]


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


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
    docker = _docker()
    checks: list[dict] = []
    pg = _port_open(25432)
    rd = _port_open(26379)

    if not docker:
        status = "RUNTIME_BLOCKED_NO_DOCKER"
    else:
        if not (pg and rd):
            up = _compose(["up", "-d"])
            checks.append(
                {
                    "name": "compose_up",
                    "returncode": up.returncode,
                    "stderr": (up.stderr or "")[-500:],
                }
            )
            wait = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "wait_qualification.py"), "--timeout", "90"],
                cwd=ROOT,
            )
            pg = _port_open(25432)
            rd = _port_open(26379)
            checks.append({"name": "wait_qualification", "returncode": wait.returncode})
        else:
            wait = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "wait_qualification.py"), "--timeout", "30"],
                cwd=ROOT,
            )
            checks.append({"name": "wait_qualification", "returncode": wait.returncode})

        if not (pg and rd):
            status = "BLOCKED"
            checks.append({"name": "ports", "postgres_25432": pg, "redis_26379": rd})
        else:
            from selene.qualification.paper_chain import run_paper_chain

            chain = run_paper_chain(environ={"EXEC_MODE": os.getenv("EXEC_MODE", "PAPER")})
            chain_ok = all(s.status == "PASS" for s in chain.stages) and chain.duplicate_side_effects == 0
            checks.append(
                {
                    "name": "scanner_to_gateway_chain",
                    "status": "PASS" if chain_ok else "FAIL",
                    "impl": "in-process PAPER (compose has postgres+redis only)",
                    "stages": [{"name": s.name, "status": s.status} for s in chain.stages],
                    "duplicate_side_effects": chain.duplicate_side_effects,
                    "exec_mode": chain.exec_mode,
                }
            )
            checks.append(
                {
                    "name": "compose_service_set",
                    "status": "PARTIAL",
                    "detail": "isolated compose does not start scanner/signal/portfolio/risk/execution/gateway containers",
                }
            )
            if chain_ok:
                status = "PARTIAL"
            else:
                status = "FAIL"

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "docker": docker,
        "postgres_25432": pg,
        "redis_26379": rd,
        "exec_mode": os.getenv("EXEC_MODE", "PAPER"),
        "limited_live": False,
        "auto_mainnet": False,
        "checks": checks,
        "note": "Never claims full compose-smoke PASS unless scanner..gateway processes are up.",
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2) + "\n")
    print(json.dumps({"status": status, "out": str(OUT)}))
    return 2 if status == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
