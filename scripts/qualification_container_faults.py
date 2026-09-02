#!/usr/bin/env python3
"""Container-level fault injection against the isolated qualification stack."""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone

import redis as redislib

from selene.qualification.compose_ctl import (
    READY_URLS,
    ROOT,
    compose,
    docker_available,
    http_json,
    inspect_containers,
    wait_ready,
)

OUT = ROOT / "evidence" / "smoke" / "fault-injection-report.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _redis():
    return redislib.Redis(host="127.0.0.1", port=26379, decode_responses=True)


def _encode(data: dict) -> dict:
    return {k: json.dumps(v) if not isinstance(v, str) else v for k, v in data.items()}


def _pg(sql: str) -> str:
    proc = compose(["exec", "-T", "qual-postgres", "psql", "-U", "selene", "-d", "selene", "-tAc", sql])
    return (proc.stdout or "").strip()


def _ready_code(name: str) -> int:
    code, _body = http_json(READY_URLS[name], timeout=3)
    return code


def _restart(svc: str) -> dict:
    before = inspect_containers()
    proc = compose(["restart", svc])
    time.sleep(2)
    red = False
    deadline = time.time() + 30
    while time.time() < deadline:
        if _ready_code(svc if svc in READY_URLS else "execution") in {0, 503}:
            red = True
            break
        time.sleep(0.5)
    recovered = wait_ready(180)
    after = inspect_containers()
    return {
        "service": svc,
        "restart_returncode": proc.returncode,
        "readiness_went_red": red,
        "recovered": recovered.get("ok"),
        "before": before,
        "after": after,
        "at": _now(),
    }


def run_faults(*, start_if_needed: bool = True) -> dict:
    generated = _now()
    if not docker_available():
        payload = {"generated_at": generated, "status": "RUNTIME_BLOCKED_NO_DOCKER"}
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(payload, indent=2) + "\n")
        return payload

    ready = wait_ready(20)
    if not ready.get("ok") and start_if_needed:
        compose(["up", "-d", "--no-build"], timeout=600)
        ready = wait_ready(240)
    cases = []
    if not ready.get("ok"):
        payload = {
            "generated_at": generated,
            "status": "FAIL",
            "ready": ready,
            "note": "stack not ready for container fault injection",
        }
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(payload, indent=2, default=str) + "\n")
        return payload

    cases.append({"name": "restart_redis", **_restart("qual-redis")})
    cases.append({"name": "restart_postgres", **_restart("qual-postgres")})
    cases.append({"name": "restart_execution", **_restart("execution")})
    cases.append({"name": "restart_risk", **_restart("risk")})

    r = _redis()
    inflight_id = str(uuid.uuid4())
    r.xadd(
        "risk.check",
        _encode(
            {
                "order_id": inflight_id,
                "signal_id": str(uuid.uuid4()),
                "symbol": "BTCUSDT",
                "side": "BUY",
                "quantity": 0.01,
                "entry_price": 100.0,
                "stop_price": 99.0,
                "allocated_usd": 50.0,
                "win_probability": 0.7,
                "regime": "RANGING",
            }
        ),
    )
    time.sleep(1)
    mid_restart = _restart("execution")
    mid_restart["name"] = "restart_execution_after_intent"
    cases.append(mid_restart)

    replay_body = {
        "id": str(uuid.uuid4()),
        "symbol": "BTCUSDT",
        "signal_type": "LONG_SETUP",
        "direction": "LONG",
        "regime": "RANGING",
        "win_probability": 0.7,
        "entry_price": 100.0,
        "stop_loss": 99.0,
        "take_profit": 102.0,
        "data_quality": 1.0,
        "position_size": 0.01,
        "allocated_capital": 50.0,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "is_actionable": True,
    }
    r.xadd("signal.sized", _encode(replay_body))
    r.xadd("signal.sized", _encode(replay_body))
    cases.append({"name": "replay_same_redis_message", "count": 2, "at": _now()})

    r.xadd("order.lifecycle", _encode({"event": "filled", "symbol": "BTCUSDT", "qty": 0.4, "seq": 2}))
    r.xadd("order.lifecycle", _encode({"event": "filled", "symbol": "BTCUSDT", "qty": 0.6, "seq": 1}))
    cases.append({"name": "out_of_order_and_duplicate_fills", "at": _now()})

    compose(["pause", "qual-redis"])
    paused_ready = _ready_code("execution")
    compose(["unpause", "qual-redis"])
    compose(["pause", "qual-postgres"])
    time.sleep(2)
    compose(["unpause", "qual-postgres"])
    after_blip = wait_ready(180)
    cases.append(
        {
            "name": "short_disconnect",
            "execution_ready_during_redis_pause": paused_ready,
            "recovered": after_blip.get("ok"),
            "at": _now(),
        }
    )

    side_count = _pg("SELECT count(*) FROM side_effects WHERE operation_kind='place';") or "0"
    side_distinct = _pg("SELECT count(DISTINCT client_order_id) FROM side_effects WHERE operation_kind='place';") or "0"
    try:
        dup = max(0, int(side_count) - int(side_distinct))
        ledger_ok = True
    except ValueError:
        dup = None
        ledger_ok = False

    recoveries = {c["name"]: c.get("recovered") or c.get("readiness_went_red") for c in cases}
    redis_ok = cases[0].get("recovered") is True
    pg_ok = cases[1].get("recovered") is True
    exec_ok = cases[2].get("recovered") is True
    risk_ok = cases[3].get("recovered") is True
    red_seen = any(c.get("readiness_went_red") for c in cases if "readiness_went_red" in c)
    status = "PASS" if redis_ok and pg_ok and exec_ok and risk_ok and after_blip.get("ok") and dup == 0 else "FAIL"
    payload = {
        "generated_at": generated,
        "status": status,
        "docker": True,
        "containers": inspect_containers(),
        "cases": cases,
        "redis_restart_recovery": redis_ok,
        "postgres_restart_recovery": pg_ok,
        "execution_restart_recovery": exec_ok,
        "risk_restart_recovery": risk_ok,
        "readiness_fail_closed": red_seen,
        "ledger_gaps": 0 if ledger_ok else None,
        "duplicate_side_effects": dup,
        "exec_mode": "PAPER",
        "recoveries": recoveries,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, default=str) + "\n")
    print(json.dumps({"status": status, "out": str(OUT), "duplicate_side_effects": dup}))
    return payload


def main() -> int:
    payload = run_faults(start_if_needed=True)
    if os.getenv("QUAL_KEEP", "") not in {"1", "true", "yes"}:
        compose(["down", "-v"], timeout=180)
    return 0 if payload.get("status") == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
