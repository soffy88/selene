#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

from selene.qualification.shadow_epoch import write_status as write_shadow_status
from selene.qualification.shadow_report import write_gap_report

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    path = write_gap_report(ROOT)
    write_shadow_status()
    payload = json.loads(path.read_text(encoding="utf-8"))
    print(
        json.dumps(
            {
                "status": payload["status"],
                "days_run": payload["days_run"],
                "regimes_seen": payload["regimes_seen"],
                "block_reasons": payload["block_reasons"],
                "out": str(path),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
