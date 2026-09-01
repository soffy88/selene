"""Fail-closed qualification aggregator (P1-3 / section 9)."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from selene.evidence.verify import ArtifactError, verify_oos, verify_release, verify_shadow


def verify_all(
    *,
    release: str | None,
    oos: str | None,
    shadow: str | None,
    fail_closed: bool = True,
) -> dict[str, Any]:
    report: dict[str, Any] = {"status": "PASS", "checks": [], "errors": []}
    for kind, path, fn in (
        ("release", release, verify_release),
        ("oos", oos, verify_oos),
        ("shadow", shadow, verify_shadow),
    ):
        if not path:
            report["checks"].append({"kind": kind, "status": "NOT_RUN"})
            if fail_closed:
                report["errors"].append(f"{kind} path missing")
                report["status"] = "NO_GO"
            continue
        try:
            fn(path)
            report["checks"].append({"kind": kind, "status": "PASS", "path": path})
        except ArtifactError as exc:
            report["checks"].append({"kind": kind, "status": "FAIL", "path": path, "error": str(exc)})
            report["errors"].append(str(exc))
            report["status"] = "NO_GO"
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release")
    parser.add_argument("--oos")
    parser.add_argument("--shadow")
    parser.add_argument("--fail-closed", action="store_true")
    args = parser.parse_args(argv)
    report = verify_all(
        release=args.release,
        oos=args.oos,
        shadow=args.shadow,
        fail_closed=args.fail_closed,
    )
    sys.stdout.write(json.dumps(report, sort_keys=True) + "\n")
    return 0 if report["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
