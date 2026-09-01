#!/usr/bin/env python3
"""P0-4 fault injection: lost ack, duplicate redis message, timeout. No duplicate submit."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from shared.ledger.side_effects import SideEffectStore, submit_once
from shared.ledger.sqlite_store import SqliteLedger

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "evidence" / "smoke" / "fault-injection.json"


class Venue:
    def __init__(self) -> None:
        self.submits = 0
        self.seen: dict[str, dict] = {}

    def submit(self, cid: str, *, timeout: bool = False) -> dict:
        self.submits += 1
        rec = {"status": "acked", "exchange_id": f"ex-{cid}"}
        self.seen[cid] = rec
        if timeout:
            raise TimeoutError("lost")
        return rec


def main() -> int:
    store = SideEffectStore(SqliteLedger(":memory:"))
    venue = Venue()
    cases = []

    rec = submit_once(
        venue="binance",
        account="acct",
        client_order_id="c1",
        operation_kind="place",
        submit_fn=lambda: venue.submit("c1", timeout=True),
        probe_fn=lambda: venue.seen.get("c1"),
        store=store,
    )
    cases.append({"name": "lost_ack_probe", "status": rec.status, "submits": venue.submits})

    submit_once(
        venue="binance",
        account="acct",
        client_order_id="c1",
        operation_kind="place",
        submit_fn=lambda: venue.submit("c1"),
        store=store,
    )
    cases.append({"name": "redis_redelivery", "submits": venue.submits})

    ok = venue.submits == 1 and rec.status == "acked"
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "PASS" if ok else "FAIL",
        "duplicate_external_submits": venue.submits - 1,
        "cases": cases,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": payload["status"], "submits": venue.submits}))
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
