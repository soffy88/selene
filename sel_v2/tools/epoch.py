"""paper_epoch mechanism (GL1 T0.5).

The validation clock for go-live readiness (D3: "epoch >= 30 days, all None-prone
feature windows full") only means something if the code that produced the epoch's
data hasn't changed underneath it (R1 code freeze). This module:

  - computes a deterministic fingerprint of the judgment-logic layer
    (sel_v2/states/** + sel_v2/strategies/**, sha256 per file + git HEAD),
  - starts a new epoch (records the fingerprint + a human-readable reason),
  - reports whether the CURRENT code still matches the fingerprint the active
    epoch started with (CLEAN) or has drifted (DIRTY) -- any strategies/** or
    states/** edit after epoch start must show DIRTY here, that's the whole point.

All verification/statistics tooling (golive_report, DSR, shadow reconciliation)
should filter to `timestamp >= (current epoch).started_at` and refuse to treat
DIRTY-epoch data as evidence.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import asyncpg

# The judgment-logic layer R1 freezes once an epoch is running (GL1 spec §1).
_FINGERPRINT_ROOTS = ("sel_v2/states", "sel_v2/strategies")
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _iter_fingerprint_files():
    for root in _FINGERPRINT_ROOTS:
        base = _REPO_ROOT / root
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            yield path


def _git_hash() -> str:
    """'unknown' (not a crash) when .git isn't present — deployed containers exclude
    it via .dockerignore, so this degrades gracefully instead of fabricating a value.
    GIT_COMMIT_SHA env var is an escape hatch for images that bake it in at build time
    (not currently set anywhere in this repo's compose/Dockerfiles)."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return os.environ.get("GIT_COMMIT_SHA", "unknown")


def compute_fingerprint() -> str:
    """sha256 over the sorted (relative_path, file_sha256) list of every .py file
    under sel_v2/states/ and sel_v2/strategies/ — deterministic regardless of
    filesystem iteration order, sensitive to any content change in those files."""
    h = hashlib.sha256()
    for path in _iter_fingerprint_files():
        rel = path.relative_to(_REPO_ROOT).as_posix()
        file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        h.update(f"{rel}:{file_hash}\n".encode())
    return h.hexdigest()


@dataclass
class EpochStatus:
    epoch_id: Optional[str]
    started_at: Optional[object]
    reason: Optional[str]
    fingerprint_at_start: Optional[str]
    current_fingerprint: str
    git_hash_at_start: Optional[str]
    current_git_hash: str
    status: str  # "CLEAN" / "DIRTY" / "NO_EPOCH"


async def start(pool: asyncpg.Pool, reason: str) -> str:
    fingerprint = compute_fingerprint()
    git_hash = _git_hash()
    async with pool.acquire() as conn:
        epoch_id = await conn.fetchval(
            """
            INSERT INTO v2_paper_epochs (code_fingerprint, git_hash, reason)
            VALUES ($1, $2, $3) RETURNING epoch_id
            """,
            fingerprint,
            git_hash,
            reason,
        )
    return str(epoch_id)


async def status(pool: asyncpg.Pool) -> EpochStatus:
    current_fp = compute_fingerprint()
    current_git = _git_hash()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT epoch_id, started_at, code_fingerprint, git_hash, reason "
            "FROM v2_paper_epochs ORDER BY started_at DESC LIMIT 1"
        )
    if row is None:
        return EpochStatus(
            epoch_id=None,
            started_at=None,
            reason=None,
            fingerprint_at_start=None,
            current_fingerprint=current_fp,
            git_hash_at_start=None,
            current_git_hash=current_git,
            status="NO_EPOCH",
        )
    dirty = row["code_fingerprint"] != current_fp
    return EpochStatus(
        epoch_id=str(row["epoch_id"]),
        started_at=row["started_at"],
        reason=row["reason"],
        fingerprint_at_start=row["code_fingerprint"],
        current_fingerprint=current_fp,
        git_hash_at_start=row["git_hash"],
        current_git_hash=current_git,
        status="DIRTY" if dirty else "CLEAN",
    )


async def _main() -> None:
    parser = argparse.ArgumentParser(description="sel_v2 paper_epoch (GL1 T0.5)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_start = sub.add_parser("start", help="start a new epoch")
    p_start.add_argument("--reason", default="", help="why this epoch was started")
    sub.add_parser("status", help="report CLEAN/DIRTY/NO_EPOCH for the current epoch")
    args = parser.parse_args()

    dsn = os.environ["DB_URL"].replace("postgresql+asyncpg://", "postgresql://")
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    try:
        if args.cmd == "start":
            epoch_id = await start(pool, args.reason)
            print(f"started epoch {epoch_id}")
        else:
            s = await status(pool)
            print(
                f"status={s.status} epoch_id={s.epoch_id} started_at={s.started_at} "
                f"reason={s.reason!r}"
            )
            if s.status == "DIRTY":
                print(f"  fingerprint_at_start={s.fingerprint_at_start}")
                print(f"  current_fingerprint ={s.current_fingerprint}")
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(_main())
