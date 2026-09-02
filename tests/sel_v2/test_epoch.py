"""Tests for sel_v2.tools.epoch (GL1 T0.5)."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import sel_v2.tools.epoch as epoch_mod


def _write_tree(tmp_path: Path):
    (tmp_path / "sel_v2" / "states").mkdir(parents=True)
    (tmp_path / "sel_v2" / "strategies").mkdir(parents=True)
    (tmp_path / "sel_v2" / "states" / "a.py").write_text("VALUE = 1\n")
    (tmp_path / "sel_v2" / "strategies" / "b.py").write_text("VALUE = 2\n")
    # non-.py file under the same roots must not affect the fingerprint
    (tmp_path / "sel_v2" / "strategies" / "README.md").write_text("docs\n")
    # __pycache__ noise must be excluded
    pycache = tmp_path / "sel_v2" / "states" / "__pycache__"
    pycache.mkdir()
    (pycache / "a.cpython-311.pyc").write_bytes(b"\x00\x01")
    return tmp_path


# ── compute_fingerprint ──────────────────────────────────────────────────────


def test_fingerprint_changes_when_strategies_file_edited(tmp_path, monkeypatch):
    root = _write_tree(tmp_path)
    monkeypatch.setattr(epoch_mod, "_REPO_ROOT", root)
    fp_before = epoch_mod.compute_fingerprint()

    (root / "sel_v2" / "strategies" / "b.py").write_text("VALUE = 999\n")
    fp_after = epoch_mod.compute_fingerprint()

    assert fp_before != fp_after


def test_fingerprint_changes_when_states_file_edited(tmp_path, monkeypatch):
    root = _write_tree(tmp_path)
    monkeypatch.setattr(epoch_mod, "_REPO_ROOT", root)
    fp_before = epoch_mod.compute_fingerprint()

    (root / "sel_v2" / "states" / "a.py").write_text("VALUE = 999\n")
    fp_after = epoch_mod.compute_fingerprint()

    assert fp_before != fp_after


def test_fingerprint_stable_across_calls_with_no_changes(tmp_path, monkeypatch):
    root = _write_tree(tmp_path)
    monkeypatch.setattr(epoch_mod, "_REPO_ROOT", root)
    assert epoch_mod.compute_fingerprint() == epoch_mod.compute_fingerprint()


def test_fingerprint_ignores_non_py_and_pycache(tmp_path, monkeypatch):
    root = _write_tree(tmp_path)
    monkeypatch.setattr(epoch_mod, "_REPO_ROOT", root)
    fp_before = epoch_mod.compute_fingerprint()

    (root / "sel_v2" / "strategies" / "README.md").write_text("changed docs\n")
    (root / "sel_v2" / "states" / "__pycache__" / "a.cpython-311.pyc").write_bytes(b"\xff")
    fp_after = epoch_mod.compute_fingerprint()

    assert fp_before == fp_after


def test_fingerprint_unaffected_by_files_outside_the_two_roots(tmp_path, monkeypatch):
    root = _write_tree(tmp_path)
    (root / "sel_v2" / "paper").mkdir(parents=True)
    (root / "sel_v2" / "paper" / "c.py").write_text("VALUE = 3\n")
    monkeypatch.setattr(epoch_mod, "_REPO_ROOT", root)
    fp_before = epoch_mod.compute_fingerprint()

    (root / "sel_v2" / "paper" / "c.py").write_text("VALUE = 4\n")
    fp_after = epoch_mod.compute_fingerprint()

    assert fp_before == fp_after


# ── start / status against a mocked pool ────────────────────────────────────


def _mock_pool(fetchval=None, fetchrow=None):
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=fetchval)
    conn.fetchrow = AsyncMock(return_value=fetchrow)
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return pool, conn


def test_start_inserts_fingerprint_and_reason():
    pool, conn = _mock_pool(fetchval="11111111-1111-1111-1111-111111111111")
    epoch_id = asyncio.run(epoch_mod.start(pool, "G0 confirmed"))
    assert epoch_id == "11111111-1111-1111-1111-111111111111"
    sql = conn.fetchval.call_args[0][0]
    assert "INSERT INTO v2_paper_epochs" in sql
    assert conn.fetchval.call_args[0][3] == "G0 confirmed"


def test_status_no_epoch():
    pool, _ = _mock_pool(fetchrow=None)
    s = asyncio.run(epoch_mod.status(pool))
    assert s.status == "NO_EPOCH"
    assert s.epoch_id is None


def test_status_clean_when_fingerprint_matches(monkeypatch):
    monkeypatch.setattr(epoch_mod, "compute_fingerprint", lambda: "abc123")
    row = {
        "epoch_id": "e1",
        "started_at": "t1",
        "code_fingerprint": "abc123",
        "git_hash": "deadbeef",
        "reason": "G0 confirmed",
    }
    pool, _ = _mock_pool(fetchrow=row)
    s = asyncio.run(epoch_mod.status(pool))
    assert s.status == "CLEAN"


def test_status_dirty_when_fingerprint_mismatches(monkeypatch):
    monkeypatch.setattr(epoch_mod, "compute_fingerprint", lambda: "NEW_FINGERPRINT")
    row = {
        "epoch_id": "e1",
        "started_at": "t1",
        "code_fingerprint": "OLD_FINGERPRINT",
        "git_hash": "deadbeef",
        "reason": "G0 confirmed",
    }
    pool, _ = _mock_pool(fetchrow=row)
    s = asyncio.run(epoch_mod.status(pool))
    assert s.status == "DIRTY"
    assert s.fingerprint_at_start == "OLD_FINGERPRINT"
    assert s.current_fingerprint == "NEW_FINGERPRINT"


# ── _git_hash graceful degradation ──────────────────────────────────────────


def test_git_hash_falls_back_when_git_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(epoch_mod, "_REPO_ROOT", tmp_path)  # no .git here
    monkeypatch.delenv("GIT_COMMIT_SHA", raising=False)
    assert epoch_mod._git_hash() == "unknown"


def test_git_hash_uses_env_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(epoch_mod, "_REPO_ROOT", tmp_path)
    monkeypatch.setenv("GIT_COMMIT_SHA", "cafef00d")
    assert epoch_mod._git_hash() == "cafef00d"
