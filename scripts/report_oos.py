#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

from selene.qualification.oos_report import write_gap_report

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    path = write_gap_report(ROOT)
    payload = json.loads(path.read_text(encoding="utf-8"))
    print(
        json.dumps(
            {
                "status": payload["status"],
                "n_trades": payload["n_trades"],
                "regimes": payload["regimes"],
                "time_range": payload["time_range"],
                "block_reasons": payload["block_reasons"],
                "out": str(path),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
