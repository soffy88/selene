#!/usr/bin/env python3
"""Wait until every isolated qualification container answers readyz."""

from __future__ import annotations

import argparse
import sys

from selene.qualification.compose_ctl import docker_available, wait_ready


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--timeout", type=float, default=180)
    args = p.parse_args()
    if not docker_available():
        sys.stderr.write("docker unavailable\n")
        return 2
    result = wait_ready(args.timeout)
    if not result["ok"]:
        sys.stderr.write(f"readiness failed: {result}\n")
        return 2
    print("qualification stack ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
