"""
Tests for paper_startup.py — pre-flight checks and report generation.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import datetime, timezone

import pytest

from sel_v2.scripts.paper_startup import (
    _build_report,
    run_checks,
    PHASE_HISTORY_SQL,
    DECISION_TRAIL_SQL,
)


# ── SQL content tests ─────────────────────────────────────────────────────────

def test_phase_history_sql_contains_strategy_1():
    assert "strategy_1" in PHASE_HISTORY_SQL
    assert "phase_0" in PHASE_HISTORY_SQL


def test_phase_history_sql_contains_strategy_2():
    assert "strategy_2" in PHASE_HISTORY_SQL
    assert "0.10" in PHASE_HISTORY_SQL  # strategy_2 kelly_cap_lower


def test_phase_history_sql_contains_insert():
    assert "INSERT INTO v2_strategy_phase_history" in PHASE_HISTORY_SQL


def test_decision_trail_sql_contains_paper_startup():
    assert "paper_startup" in DECISION_TRAIL_SQL
    assert "paper_engine" in DECISION_TRAIL_SQL


def test_decision_trail_sql_insert():
    assert "INSERT INTO v2_decision_trail" in DECISION_TRAIL_SQL


# ── Dry-run checks ────────────────────────────────────────────────────────────

def test_run_checks_dry_run_returns_list():
    results = asyncio.run(run_checks(db_url=None))
    assert isinstance(results, list)
    assert len(results) >= 3  # at least bars, params, lob


def test_run_checks_dry_run_all_dicts():
    results = asyncio.run(run_checks(db_url=None))
    for r in results:
        assert isinstance(r, dict)


def test_run_checks_dry_run_helixa_expected_ng():
    """helixa GRANT not applied is expected NG, not a blocker."""
    results = asyncio.run(run_checks(db_url=None))
    helixa = [r for r in results if "helixa" in str(r.get("source", ""))]
    if helixa:
        assert helixa[0].get("ok") is False  # expected NG


def test_run_checks_dry_run_v2_bars_4h_present():
    results = asyncio.run(run_checks(db_url=None))
    tables = [r.get("table") for r in results]
    assert "v2_bars_4h" in tables


# ── Report build tests ────────────────────────────────────────────────────────

def test_build_report_returns_markdown():
    now = datetime(2026, 4, 29, tzinfo=timezone.utc)
    dry_results = asyncio.run(run_checks(db_url=None))
    md = _build_report(now, dry_results)
    assert isinstance(md, str)
    assert len(md) > 200


def test_build_report_contains_sql_blocks():
    now = datetime(2026, 4, 29, tzinfo=timezone.utc)
    dry_results = asyncio.run(run_checks(db_url=None))
    md = _build_report(now, dry_results)
    assert "INSERT INTO v2_strategy_phase_history" in md
    assert "INSERT INTO v2_decision_trail" in md


def test_build_report_contains_checklist():
    now = datetime(2026, 4, 29, tzinfo=timezone.utc)
    dry_results = asyncio.run(run_checks(db_url=None))
    md = _build_report(now, dry_results)
    assert "前提条件チェック" in md
    assert "v2_bars_4h" in md


def test_build_report_ng_status_for_dry_run():
    """Dry-run should show NG for most checks (no DB)."""
    now = datetime(2026, 4, 29, tzinfo=timezone.utc)
    dry_results = asyncio.run(run_checks(db_url=None))
    md = _build_report(now, dry_results)
    assert "❌ NG" in md


def test_build_report_date_in_header():
    now = datetime(2026, 4, 29, tzinfo=timezone.utc)
    dry_results = asyncio.run(run_checks(db_url=None))
    md = _build_report(now, dry_results)
    assert "2026-04-29" in md
