"""Guard: every column the readers SELECT from v2_strategy_params must exist in
the committed schema.sql.

Regression test for the P0-2 fix. The schema had defined v2_strategy_params with
a *versioned* shape (strategy / param_name / valid_from / valid_to) while every
reader queried a *flat* shape (param_key / param_value). A fresh deploy therefore
created a table the readers could not query ("column param_key does not exist"),
and the system only worked because a hand-created flat table existed on the live
DB. The existing schema-collector consistency test only scans INSERTs in
sel_v2/data/, so it never caught this reader-side drift.

No database is required — this parses SQL text on both sides.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = ROOT / "sel_v2" / "db" / "schema.sql"

# Files that read v2_strategy_params (SELECT ... FROM it).
READER_FILES = [
    ROOT / "sel_v2" / "strategies" / "params_loader.py",
    ROOT / "sel_v2" / "paper" / "paper_engine.py",
    ROOT / "sel_v2" / "scripts" / "paper_startup.py",
]

TABLE = "v2_strategy_params"


def _schema_columns(table: str) -> set[str]:
    sql = SCHEMA.read_text()
    chunk = sql.split(f"CREATE TABLE IF NOT EXISTS {table}", 1)[1]
    body = chunk.split("(", 1)[1]
    depth, src = 1, []
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                break
        src.append(ch)
    clean = "\n".join(ln.split("--", 1)[0] for ln in "".join(src).splitlines())
    cols = set()
    for line in clean.split(","):
        line = line.strip()
        if not line:
            continue
        token = line.split()[0]
        if token.upper() in {"PRIMARY", "UNIQUE", "FOREIGN", "CONSTRAINT", "CHECK"}:
            continue
        cols.add(token.lower())
    return cols


def _selected_columns() -> set[str]:
    """Columns referenced in `SELECT <cols> FROM v2_strategy_params` across readers.

    The column list is restricted to a plain identifier/comma run so the pattern
    cannot bleed across statements into surrounding code. Aggregates like
    count(*) and a bare * select no named column and are ignored.
    """
    pat = re.compile(r"SELECT\s+([\w\s,]+?)\s+FROM\s+" + TABLE, re.IGNORECASE)
    cols: set[str] = set()
    for path in READER_FILES:
        for match in pat.finditer(path.read_text()):
            for raw in match.group(1).split(","):
                name = raw.strip().split()[0].lower()
                if name in {"*", ""} or "(" in name:
                    continue
                cols.add(name)
    return cols


def test_readers_match_schema():
    schema_cols = _schema_columns(TABLE)
    selected = _selected_columns()
    assert selected, "no SELECT ... FROM v2_strategy_params found in reader files"
    missing = selected - schema_cols
    assert not missing, (
        f"readers SELECT columns absent from schema.sql {TABLE}: {sorted(missing)}. "
        f"schema has {sorted(schema_cols)}, readers want {sorted(selected)}"
    )


def test_flat_key_value_shape():
    """The flat contract the readers and writer depend on."""
    cols = _schema_columns(TABLE)
    assert "param_key" in cols
    assert "param_value" in cols
    # the old versioned columns must not silently reappear
    assert "param_name" not in cols, "v2_strategy_params reverted to the versioned param_name shape that breaks readers"
