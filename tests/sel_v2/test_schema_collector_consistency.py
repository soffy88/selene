"""Guard: every v2 collector INSERT must reference columns that exist in schema.sql.

Regression test for optimization item #1 — the committed schema had diverged
from the collector INSERTs, so every INSERT silently failed and the v2_* raw
tables stayed empty. This test parses both sides and fails if they drift again.
No database is required.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = ROOT / "sel_v2" / "db" / "schema.sql"
DATA_DIR = ROOT / "sel_v2" / "data"


def _parse_schema_tables(sql: str) -> dict[str, set[str]]:
    """Return {table_name: {column, ...}} from CREATE TABLE blocks."""
    tables: dict[str, set[str]] = {}
    # Block extraction: split on "CREATE TABLE IF NOT EXISTS", then read the
    # balanced parenthesised column list for each table.
    for chunk in sql.split("CREATE TABLE IF NOT EXISTS")[1:]:
        name = chunk.split("(", 1)[0].strip()
        body = chunk.split("(", 1)[1]
        # body runs until the matching ");" that closes the column list
        depth = 1
        cols_src = []
        for ch in body:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    break
            cols_src.append(ch)
        # Strip inline "-- ..." comments before tokenising columns.
        clean = "\n".join(ln.split("--", 1)[0] for ln in "".join(cols_src).splitlines())
        cols = set()
        for line in clean.split(","):
            line = line.strip()
            if not line:
                continue
            token = line.split()[0]
            # skip table-level constraints
            if token.upper() in {"PRIMARY", "UNIQUE", "FOREIGN", "CONSTRAINT", "CHECK"}:
                continue
            cols.add(token.lower())
        tables[name] = cols
    return tables


def _parse_collector_inserts() -> list[tuple[str, str, list[str]]]:
    """Return [(file, table, [columns]), ...] for every INSERT INTO in collectors."""
    out = []
    pat = re.compile(r"INSERT INTO\s+(\w+)\s*\(([^)]*)\)", re.IGNORECASE)
    for py in sorted(DATA_DIR.glob("*.py")):
        text = py.read_text()
        for m in pat.finditer(text):
            table = m.group(1)
            cols = [c.strip().lower() for c in m.group(2).split(",") if c.strip()]
            out.append((py.name, table, cols))
    return out


SCHEMA_TABLES = _parse_schema_tables(SCHEMA.read_text())
INSERTS = _parse_collector_inserts()


def test_schema_has_v2_raw_tables():
    for t in ("v2_ticks", "v2_lob_snapshots", "v2_derivatives_snapshots", "v2_liquidations", "v2_bars_4h"):
        assert t in SCHEMA_TABLES, f"{t} missing from schema.sql"


def test_inserts_found():
    # We expect at least the five raw-table collectors to be parsed.
    tables = {t for _, t, _ in INSERTS}
    assert {"v2_ticks", "v2_lob_snapshots", "v2_derivatives_snapshots", "v2_liquidations", "v2_bars_4h"}.issubset(
        tables
    )


@pytest.mark.parametrize("fname,table,cols", INSERTS, ids=[f"{f}:{t}" for f, t, _ in INSERTS])
def test_insert_columns_exist_in_schema(fname, table, cols):
    assert table in SCHEMA_TABLES, f"{fname} inserts into unknown table {table}"
    schema_cols = SCHEMA_TABLES[table]
    missing = [c for c in cols if c not in schema_cols]
    assert not missing, (
        f"{fname} INSERT INTO {table} references columns not in schema.sql: {missing}\n"
        f"schema columns: {sorted(schema_cols)}"
    )
