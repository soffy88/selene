"""SQLite-backed durable ledger. Used for gateway idempotency and venue side-effects.

PostgreSQL remains the production authority; this file is the portable implementation
that tests and local PAPER use. Schema matches infra/timescaledb/ledger.sql.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS write_idempotency (
    request_id TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    actor TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    body_json TEXT NOT NULL,
    stored_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS side_effects (
    venue TEXT NOT NULL,
    account TEXT NOT NULL,
    client_order_id TEXT NOT NULL,
    operation_kind TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (venue, account, client_order_id, operation_kind)
);
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    actor TEXT NOT NULL,
    request_id TEXT,
    reason TEXT,
    path TEXT,
    git_sha TEXT,
    payload_json TEXT NOT NULL,
    at TEXT NOT NULL
);
"""


class SqliteLedger:
    def __init__(self, path: Optional[str] = None) -> None:
        raw = path if path is not None else os.getenv("SELENE_LEDGER_PATH", "")
        self.path = raw.strip() or ":memory:"
        self._lock = threading.Lock()
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # Isolated in-memory connections do not share state; keep one connection.
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def remember_write(self, request_id: str, *, status_code: int, body: Any, path: str, actor: str) -> None:
        with self._lock:
            existing = self._conn.execute(
                "SELECT request_id FROM write_idempotency WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if existing:
                return
            self._conn.execute(
                """
                INSERT INTO write_idempotency
                    (request_id, path, actor, status_code, body_json, stored_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (request_id, path, actor, status_code, json.dumps(body), _utcnow()),
            )
            self._conn.commit()

    def lookup_write(self, request_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM write_idempotency WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "status_code": int(row["status_code"]),
            "body": json.loads(row["body_json"]),
            "path": row["path"],
            "actor": row["actor"],
            "stored_at": row["stored_at"],
        }

    def upsert_side_effect(
        self,
        *,
        venue: str,
        account: str,
        client_order_id: str,
        operation_kind: str,
        status: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        now = _utcnow()
        key = (venue, account, client_order_id, operation_kind)
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM side_effects
                WHERE venue=? AND account=? AND client_order_id=? AND operation_kind=?
                """,
                key,
            ).fetchone()
            if row is None:
                self._conn.execute(
                    """
                    INSERT INTO side_effects
                        (venue, account, client_order_id, operation_kind, status, payload_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (*key, status, json.dumps(payload), now, now),
                )
            else:
                self._conn.execute(
                    """
                    UPDATE side_effects
                    SET status=?, payload_json=?, updated_at=?
                    WHERE venue=? AND account=? AND client_order_id=? AND operation_kind=?
                    """,
                    (status, json.dumps(payload), now, *key),
                )
            self._conn.commit()
            row = self._conn.execute(
                """
                SELECT * FROM side_effects
                WHERE venue=? AND account=? AND client_order_id=? AND operation_kind=?
                """,
                key,
            ).fetchone()
        return dict(row)

    def get_side_effect(
        self, venue: str, account: str, client_order_id: str, operation_kind: str
    ) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM side_effects
                WHERE venue=? AND account=? AND client_order_id=? AND operation_kind=?
                """,
                (venue, account, client_order_id, operation_kind),
            ).fetchone()
        return dict(row) if row else None

    def record_audit(self, event: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO audit_events
                    (kind, actor, request_id, reason, path, git_sha, payload_json, at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.get("kind", "write"),
                    event.get("actor", "unknown"),
                    event.get("request_id"),
                    event.get("reason"),
                    event.get("path"),
                    event.get("git_sha"),
                    json.dumps(event.get("payload") or {}),
                    event.get("at") or _utcnow(),
                ),
            )
            self._conn.commit()


_DEFAULT: Optional[SqliteLedger] = None
_DEFAULT_LOCK = threading.Lock()


def get_sqlite_ledger() -> SqliteLedger:
    global _DEFAULT
    with _DEFAULT_LOCK:
        if _DEFAULT is None:
            _DEFAULT = SqliteLedger()
        return _DEFAULT
