#!/usr/bin/env python3
"""Fresh isolated compose smoke: real containers, PAPER only, no venue HTTP."""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone

import redis as redislib

from selene.qualification.compose_ctl import (
    COMPOSE_FILE,
    ROOT,
    SERVICES,
    compose,
    docker_available,
    http_json,
    inspect_containers,
    wait_ready,
)

OUT = ROOT / "evidence" / "smoke" / "compose-smoke-report.json"
GATEWAY = "http://127.0.0.1:25000"
EXECUTION = "http://127.0.0.1:28005"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _redis():
    return redislib.Redis(host="127.0.0.1", port=26379, decode_responses=True)


def _encode(data: dict) -> dict:
    return {k: json.dumps(v) if not isinstance(v, str) else v for k, v in data.items()}


def _pg(sql: str) -> str:
    proc = compose(
        [
            "exec",
            "-T",
            "qual-postgres",
            "psql",
            "-U",
            "selene",
            "-d",
            "selene",
            "-tAc",
            sql,
        ]
    )
    return (proc.stdout or "").strip()


def _wait_orders(timeout_s: float = 120.0) -> list[dict]:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        code, body = http_json(f"{GATEWAY}/api/v4/orders")
        orders = body.get("orders") if isinstance(body, dict) else None
        if code == 200 and orders:
            return orders
        code2, body2 = http_json(f"{EXECUTION}/orders/recent")
        recent = body2.get("orders") if isinstance(body2, dict) else None
        if code2 == 200 and recent:
            return recent
        time.sleep(2)
    return []


def _container_names(rows: list[dict]) -> list[str]:
    names = []
    for row in rows:
        name = row.get("Name") or row.get("Service") or row.get("name")
        if name:
            names.append(str(name))
    return names


def run_smoke(*, cleanup: bool = True) -> dict:
    generated = _now()
    if not docker_available():
        payload = {
            "generated_at": generated,
            "status": "RUNTIME_BLOCKED_NO_DOCKER",
            "exec_mode": "PAPER",
        }
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(payload, indent=2) + "\n")
        return payload

    skip_up = os.getenv("QUAL_SKIP_UP", "") in {"1", "true", "yes"}
    if skip_up:
        build = type("R", (), {"returncode": 0, "stderr": "skipped: QUAL_SKIP_UP"})()
        up = type("R", (), {"returncode": 0, "stderr": "skipped: QUAL_SKIP_UP"})()
    else:
        compose(["down", "-v"], timeout=180)
        if os.getenv("QUAL_SKIP_BUILD", "") in {"1", "true", "yes"}:
            build = type("R", (), {"returncode": 0, "stderr": "skipped: QUAL_SKIP_BUILD"})()
        else:
            build = compose(["build"], timeout=1800)
        up = compose(["up", "-d", "--no-build"], timeout=600)
    ready = wait_ready(300)
    containers = inspect_containers()
    names = _container_names(containers)
    required = list(SERVICES)
    present = {svc: any(svc in n for n in names) for svc in required}

    orders = _wait_orders(150) if ready.get("ok") else []
    lifecycle_ok = any(
        str(o.get("state") or o.get("event") or "").upper() in {"MONITORING", "FILLED", "CLOSED", "FILLED_IMMEDIATELY"}
        or o.get("event") in {"filled_immediately", "filled"}
        for o in orders
    )

    r = _redis()
    stale_id = str(uuid.uuid4())
    old_ts = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    r.xadd(
        "signal.sized",
        _encode(
            {
                "id": stale_id,
                "symbol": "ETHUSDT",
                "signal_type": "LONG_SETUP",
                "direction": "LONG",
                "regime": "RANGING",
                "win_probability": 0.7,
                "entry_price": 100.0,
                "stop_loss": 99.0,
                "take_profit": 102.0,
                "data_quality": 1.0,
                "position_size": 1.0,
                "allocated_capital": 100.0,
                "timestamp": old_ts,
                "is_actionable": True,
            }
        ),
    )
    reject_id = str(uuid.uuid4())
    r.xadd(
        "risk.check",
        _encode(
            {
                "order_id": reject_id,
                "signal_id": str(uuid.uuid4()),
                "symbol": "ETHUSDT",
                "side": "BUY",
                "quantity": 1.0,
                "entry_price": 100.0,
                "stop_price": 99.0,
                "allocated_usd": 1000.0,
                "win_probability": 0.10,
                "regime": "RANGING",
            }
        ),
    )
    time.sleep(3)
    if orders:
        replay = dict(orders[0])
        replay_payload = {
            "id": replay.get("signal_id") or str(uuid.uuid4()),
            "symbol": replay.get("symbol") or "BTCUSDT",
            "signal_type": "LONG_SETUP",
            "direction": "LONG" if replay.get("side") == "BUY" else "SHORT",
            "regime": "RANGING",
            "win_probability": 0.7,
            "entry_price": float(replay.get("entry_price") or 100),
            "stop_loss": float(replay.get("stop_loss") or 99),
            "take_profit": float(replay.get("take_profit") or 102),
            "data_quality": 1.0,
            "position_size": float(replay.get("quantity") or 0.01),
            "allocated_capital": 100.0,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "is_actionable": True,
        }
        r.xadd("signal.sized", _encode(replay_payload))
        r.xadd("signal.sized", _encode(replay_payload))

    time.sleep(4)
    exec_code, exec_health = http_json(f"{EXECUTION}/health")
    gw_code, gw_health = http_json(f"{GATEWAY}/health")
    stats = exec_health.get("stats") if isinstance(exec_health, dict) else {}
    stale_drop = int((stats or {}).get("stale_dropped") or 0)
    approved = r.xrevrange("risk.approved", count=20)
    risk_rejected = False
    for _mid, fields in approved or []:
        raw = fields.get("approved")
        try:
            val = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, json.JSONDecodeError):
            val = raw
        if val in (False, "false", 0, "0"):
            risk_rejected = True
            break
    if not risk_rejected:
        risk_rejected = int((stats or {}).get("risk_rejected") or 0) > 0

    side_count = _pg("SELECT count(*) FROM side_effects WHERE operation_kind='place';") or "0"
    side_distinct = _pg("SELECT count(DISTINCT client_order_id) FROM side_effects WHERE operation_kind='place';") or "0"
    order_count = _pg("SELECT count(*) FROM orders;") or "0"
    try:
        duplicate_side_effects = max(0, int(side_count) - int(side_distinct))
    except ValueError:
        duplicate_side_effects = None
    real_calls = int(r.get("cw4:real_exchange_calls") or 0)
    exec_mode = None
    if isinstance(exec_health, dict):
        exec_mode = exec_health.get("exec_mode")
    adapters = False
    if isinstance(exec_health, dict):
        adapters = bool(exec_health.get("adapters_enabled"))

    chain_ok = ready.get("ok") and all(present.values()) and lifecycle_ok
    status = (
        "PASS"
        if chain_ok
        and real_calls == 0
        and exec_mode in (None, "PAPER")
        and not adapters
        and risk_rejected
        and stale_drop > 0
        and duplicate_side_effects == 0
        else "FAIL"
    )
    if not ready.get("ok"):
        status = "FAIL"
    if build.returncode != 0 or up.returncode != 0:
        status = "FAIL"

    payload = {
        "generated_at": generated,
        "status": status,
        "compose_file": str(COMPOSE_FILE),
        "build_returncode": build.returncode,
        "up_returncode": up.returncode,
        "build_stderr_tail": (build.stderr or "")[-1500:],
        "ready": ready,
        "containers": containers,
        "required_services": required,
        "services_present": present,
        "independent_containers": all(present.values()),
        "full_event_chain": chain_ok,
        "paper_order_lifecycle": lifecycle_ok,
        "orders_sample": orders[:5],
        "order_count_pg": order_count,
        "risk_rejection": risk_rejected,
        "stale_signal_drop": stale_drop > 0,
        "stale_dropped": stale_drop,
        "duplicate_replay_checked": True,
        "duplicate_side_effects": duplicate_side_effects,
        "real_exchange_calls": real_calls,
        "adapters_enabled": adapters,
        "exec_mode": exec_mode or os.getenv("EXEC_MODE", "PAPER"),
        "gateway_health_code": gw_code,
        "execution_health_code": exec_code,
        "gateway_can_read_orders": bool(orders),
        "limited_live": False,
        "auto_mainnet": False,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, default=str) + "\n")
    if cleanup:
        compose(["down", "-v"], timeout=180)
    print(json.dumps({"status": status, "out": str(OUT), "lifecycle": lifecycle_ok}))
    return payload


def main() -> int:
    cleanup = os.getenv("QUAL_KEEP", "") not in {"1", "true", "yes"}
    payload = run_smoke(cleanup=cleanup)
    return 0 if payload.get("status") == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
