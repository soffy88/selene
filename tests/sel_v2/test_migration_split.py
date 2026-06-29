"""Schema-statement split tests (deploy-crash regression).

A '--' comment containing a ';' previously produced a comment-only fragment,
and executing that via asyncpg crashed the collector on startup. The splitter
must strip comments before splitting on ';'.
"""
from pathlib import Path

from sel_v2.db.migrations import _split_statements

ROOT = Path(__file__).resolve().parents[2]


def test_no_comment_only_or_empty_fragments():
    sql = (ROOT / "sel_v2" / "db" / "schema.sql").read_text()
    stmts = _split_statements(sql)
    assert stmts
    for s in stmts:
        assert s.strip()
        assert not s.lstrip().startswith("--"), f"comment-only fragment: {s[:50]!r}"


def test_semicolon_inside_comment_does_not_split():
    sql = (
        "CREATE TABLE a (x int);\n"
        "-- note: compress after 2 days; then retain\n"
        "ALTER TABLE a SET (y);\n"
    )
    stmts = _split_statements(sql)
    assert stmts == ["CREATE TABLE a (x int)", "ALTER TABLE a SET (y)"]


def test_inline_comment_stripped_but_statement_kept():
    sql = "CREATE TABLE a (\n  x int  -- the x; column\n);\n"
    stmts = _split_statements(sql)
    assert len(stmts) == 1
    assert stmts[0].startswith("CREATE TABLE a")
    assert "--" not in stmts[0]
