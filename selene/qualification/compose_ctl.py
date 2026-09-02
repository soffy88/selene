"""Docker compose helpers for the isolated qualification stack."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = ROOT / "docker-compose.qualification.yml"
COMPOSE = ["docker", "compose", "-f", str(COMPOSE_FILE), "--project-name", "selene-qualification"]

SERVICES = (
    "qual-postgres",
    "qual-redis",
    "deterministic-fixture",
    "scanner",
    "signal",
    "portfolio",
    "risk",
    "execution",
    "gateway",
)
READY_URLS = {
    "deterministic-fixture": "http://127.0.0.1:28090/readyz",
    "scanner": "http://127.0.0.1:28001/readyz",
    "signal": "http://127.0.0.1:28002/readyz",
    "portfolio": "http://127.0.0.1:28003/readyz",
    "risk": "http://127.0.0.1:28004/readyz",
    "execution": "http://127.0.0.1:28005/readyz",
    "gateway": "http://127.0.0.1:25000/readyz",
}


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"], capture_output=True, timeout=45, check=True
        )
        return True
    except Exception:
        try:
            subprocess.run(["docker", "ps", "-q"], capture_output=True, timeout=45, check=True)
            return True
        except Exception:
            return False


def compose(args: list[str], timeout: int = 600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*COMPOSE, *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def http_json(url: str, timeout: float = 5.0) -> tuple[int, Any]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read().decode()
            try:
                return resp.status, json.loads(body)
            except json.JSONDecodeError:
                return resp.status, body
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode() if exc.fp else ""
        try:
            return exc.code, json.loads(raw) if raw else {"error": str(exc)}
        except json.JSONDecodeError:
            return exc.code, {"error": raw or str(exc)}
    except Exception as exc:
        return 0, {"error": str(exc)}


def wait_ready(timeout_s: float = 180.0) -> dict[str, Any]:
    deadline = time.time() + timeout_s
    last: dict[str, Any] = {}
    while time.time() < deadline:
        ok = True
        last = {}
        for name, url in READY_URLS.items():
            code, body = http_json(url)
            ready = code == 200 and (not isinstance(body, dict) or body.get("ready", True) is not False)
            last[name] = {"code": code, "ready": ready, "body": body}
            if not ready:
                ok = False
        if ok:
            return {"ok": True, "services": last}
        time.sleep(2)
    return {"ok": False, "services": last}


def inspect_containers() -> list[dict[str, Any]]:
    proc = compose(["ps", "--format", "json"])
    rows = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if rows:
        return rows
    proc = compose(["ps"])
    return [{"raw": proc.stdout, "returncode": proc.returncode}]
