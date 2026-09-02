#!/usr/bin/env python3
"""P1-5 merge-readiness for PR #8 and #9. Never merges. Never shares a working head."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "evidence" / "closure" / "pr-8-9-merge-readiness.json"


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=ROOT, capture_output=True, text=True)


def _pr(n: int) -> dict[str, Any]:
    proc = _run(
        [
            "gh",
            "pr",
            "view",
            str(n),
            "--json",
            "number,title,url,headRefName,headRefOid,baseRefName,baseRefOid,mergeable,isDraft,state",
        ]
    )
    if proc.returncode != 0:
        return {"number": n, "error": proc.stderr.strip() or proc.stdout.strip()}
    return json.loads(proc.stdout)


def main() -> int:
    pr8 = _pr(8)
    pr9 = _pr(9)
    head8 = pr8.get("headRefOid")
    head9 = pr9.get("headRefOid")
    shared = bool(head8 and head9 and head8 == head9)
    same_branch = pr8.get("headRefName") and pr8.get("headRefName") == pr9.get("headRefName")
    gates = []
    if shared:
        gates.append("SHARED_HEAD: PR #8 and #9 point at the same commit; rebase/CI must use isolated refs")
    if same_branch:
        gates.append("SHARED_BRANCH: both PRs use feat/selene-md-adapter; do not push a shared rebase")
    if pr8.get("baseRefName") != "main":
        gates.append(f"PR8_BASE={pr8.get('baseRefName')} (expected main after rebase onto latest main)")
    if pr9.get("baseRefName") not in {"main", "wave/exec-s"}:
        gates.append(f"PR9_BASE={pr9.get('baseRefName')}")
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "OWNER_BLOCKED",
        "merged": False,
        "pr8": pr8,
        "pr9": pr9,
        "shared_head": shared,
        "shared_branch": same_branch,
        "failed_gates": gates,
        "note": "Do not merge. Owner must choose independent heads after isolated rebase+CI.",
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": payload["status"], "shared_head": shared, "failed_gates": gates, "out": str(OUT)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
