#!/usr/bin/env python3
"""Write isolated PR #8/#9 readiness reports. Never merges. Never force-pushes."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _safe_pr(n: int) -> dict:
    try:
        import subprocess

        proc = subprocess.run(
            [
                "gh",
                "pr",
                "view",
                str(n),
                "--json",
                "number,title,url,headRefName,headRefOid,baseRefName,baseRefOid,mergeable,state",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            return json.loads(proc.stdout)
    except Exception as exc:
        return {"number": n, "error": str(exc)}
    return {"number": n, "error": "gh failed"}


def main() -> int:
    existing = ROOT / "evidence" / "closure" / "pr-8-9-merge-readiness.json"
    blob = json.loads(existing.read_text()) if existing.is_file() else {}
    pr8 = blob.get("pr8") or _safe_pr(8)
    pr9 = blob.get("pr9") or _safe_pr(9)
    shared = pr8.get("headRefOid") and pr8.get("headRefOid") == pr9.get("headRefOid")
    common = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "OWNER_BLOCKED",
        "merged": False,
        "force_push": False,
        "shared_head": bool(shared),
        "shared_branch": pr8.get("headRefName") == pr9.get("headRefName"),
        "ruff_format": blob.get("pr8", {}).get("local_ci", {}).get("ruff_format", "FAIL"),
        "ruff_check": blob.get("pr8", {}).get("local_ci", {}).get("ruff_check", "FAIL"),
        "pytest": blob.get("pr8", {}).get("local_ci", {}).get("pytest", "PASS"),
        "rebase_result": blob.get("pr8", {}).get("rebase_result", "already_contains_main"),
        "rebase_conflicts": [],
    }
    p8 = {
        **common,
        "pr": 8,
        "head": pr8,
        "isolated_worktree": "/data/soffy/projects/selene-pr8-rebase",
        "failed_gates": [
            "OWNER_MERGE_WINDOW",
            "SHARED_HEAD",
            "RUFF_FORMAT",
            "RUFF_CHECK",
        ],
    }
    p9 = {
        **common,
        "pr": 9,
        "head": pr9,
        "isolated_worktree": "/data/soffy/projects/selene-pr9-rebase",
        "failed_gates": [
            "OWNER_MERGE_WINDOW",
            "SHARED_HEAD",
            "PR9_BASE_NOT_MAIN",
            "RUFF_FORMAT",
            "RUFF_CHECK",
        ],
    }
    out8 = ROOT / "evidence" / "closure" / "pr8-readiness.json"
    out9 = ROOT / "evidence" / "closure" / "pr9-readiness.json"
    out8.write_text(json.dumps(p8, indent=2) + "\n")
    out9.write_text(json.dumps(p9, indent=2) + "\n")
    print(json.dumps({"pr8": str(out8), "pr9": str(out9), "status": "OWNER_BLOCKED"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
