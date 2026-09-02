#!/usr/bin/env python3
"""Wait until isolated qualification postgres/redis accept traffic and schema is applied."""

from __future__ import annotations

import argparse
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ["docker", "compose", "-f", str(ROOT / "docker-compose.qualification.yml")]


def wait_port(host: str, port: int, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=2):
                return True
        except OSError:
            time.sleep(0.5)
    return False


def _compose_ok() -> bool:
    return shutil.which("docker") is not None


def wait_schema(timeout: float) -> bool:
    if not _compose_ok():
        return False
    deadline = time.time() + timeout
    sql = "SELECT count(*) FROM signals WHERE id = '00000000-0000-4000-8000-000000000001';"
    while time.time() < deadline:
        proc = subprocess.run(
            [*COMPOSE, "exec", "-T", "qual-postgres", "psql", "-U", "selene", "-d", "selene", "-tAc", sql],
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
        if proc.returncode == 0 and proc.stdout.strip() == "1":
            return True
        time.sleep(0.5)
    return False


def wait_redis(timeout: float) -> bool:
    if not _compose_ok():
        return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        proc = subprocess.run(
            [*COMPOSE, "exec", "-T", "qual-redis", "redis-cli", "ping"],
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
        if proc.returncode == 0 and proc.stdout.strip() == "PONG":
            return True
        time.sleep(0.5)
    return False


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--timeout", type=float, default=90)
    args = p.parse_args()
    ok_pg = wait_port("127.0.0.1", 25432, args.timeout)
    ok_rd = wait_port("127.0.0.1", 26379, args.timeout)
    if not (ok_pg and ok_rd):
        sys.stderr.write(f"readiness failed postgres={ok_pg} redis={ok_rd}\n")
        return 2
    schema_ok = wait_schema(args.timeout)
    redis_ok = wait_redis(min(args.timeout, 20))
    if not (schema_ok and redis_ok):
        sys.stderr.write(f"schema/redis failed schema={schema_ok} redis={redis_ok}\n")
        return 2
    print("qualification postgres+redis ready (schema applied)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
