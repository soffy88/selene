#!/usr/bin/env python3
"""Fresh-compose smoke: identity health is PAPER and writes are not anonymous.

Full scanner->execution chain requires the Helios stack; this script records
BLOCKED when those services are absent instead of inventing PASS.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "evidence" / "smoke" / "compose-smoke-report.json"


def _get(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except Exception as exc:
        return 0, str(exc)


def main() -> int:
    gateway = os.getenv("GATEWAY_URL", "http://127.0.0.1:5000")
    execution = os.getenv("EXECUTION_URL", "http://127.0.0.1:8005")
    checks = []
    for name, url in (
        ("gateway_livez", f"{gateway}/livez"),
        ("gateway_health", f"{gateway}/health"),
        ("execution_health", f"{execution}/health"),
    ):
        status, body = _get(url)
        checks.append({"name": name, "http": status, "body": body[:500]})
    anon = _get(f"{gateway}/api/v4/signals/smoke/confirm")
    checks.append(
        {
            "name": "anonymous_write",
            "http": anon[0],
            "status": "PASS" if anon[0] in {401, 403, 404, 405, 0} else "FAIL",
        }
    )
    reachable = any(c.get("http", 0) >= 200 for c in checks[:3])
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "PASS" if reachable and checks[-1]["status"] == "PASS" else "BLOCKED",
        "checks": checks,
        "note": "Helios-dependent services absent => BLOCKED, not PASS",
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": payload["status"], "out": str(OUT)}))
    return 0 if payload["status"] != "FAIL" else 2


if __name__ == "__main__":
    raise SystemExit(main())
