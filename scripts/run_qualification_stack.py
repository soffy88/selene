#!/usr/bin/env python3
"""Build, smoke, fault-inject, and tear down the isolated qualification stack."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    env["QUAL_KEEP"] = "1"
    env["EXEC_MODE"] = "PAPER"
    py = sys.executable
    subprocess.run([py, str(ROOT / "scripts" / "report_oos.py")], cwd=ROOT, env=env, check=False)
    subprocess.run(
        [py, "-c", "from selene.qualification.shadow_epoch import write_status; write_status()"],
        cwd=ROOT,
        env=env,
        check=False,
    )
    subprocess.run(
        [py, "-c", "from selene.qualification.oos_status import write_oos_status; write_oos_status()"],
        cwd=ROOT,
        env=env,
        check=False,
    )
    smoke = subprocess.run([py, str(ROOT / "scripts" / "qualification_compose_smoke.py")], cwd=ROOT, env=env)
    faults = subprocess.run([py, str(ROOT / "scripts" / "qualification_container_faults.py")], cwd=ROOT, env=env)
    subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(ROOT / "docker-compose.qualification.yml"),
            "--project-name",
            "selene-qualification",
            "down",
            "-v",
        ],
        cwd=ROOT,
        check=False,
    )
    print(json.dumps({"smoke_rc": smoke.returncode, "faults_rc": faults.returncode}))
    return 0 if smoke.returncode == 0 and faults.returncode == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
