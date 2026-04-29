"""
paper_startup.py — one-shot paper trading startup script  (v2.1 §3.6).

Runs the pre-flight checklist and generates startup_YYYY-MM-DD.md.

⚠ DO NOT EXECUTE until Wiki authorises paper trading start:
  - Wave 5 implementation complete                       ✅  (now)
  - LOB collector running ≥ 30 days                     ⏳  (≈ 2026-05-29)
  - Helixa live trading stable ≥ 3 months               ⏳  (Wiki decision)
  - Wiki + Claude dual review passed                     ⏳

How to run (when authorised):
    cd ~/projects/selene
    .venv/bin/python -m sel_v2.scripts.paper_startup [--db-url postgresql://...]

The script:
  1. Connects to TimescaleDB (or uses mock mode if --dry-run)
  2. Checks pre-flight conditions
  3. Prints prepared SQL for phase_history initial records (does NOT execute)
  4. Prints prepared SQL for decision_trail startup event (does NOT execute)
  5. Writes startup report to sel_v2/reports/paper_startup_YYYY-MM-DD.md
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

_REPORT_DIR = os.path.join(os.path.dirname(__file__), "..", "reports")
_MIN_BARS_30D = 180  # 30 days × 6 bars/day

# ── SQL templates (NOT auto-executed — Wiki must execute via psql / DB tool) ──

PHASE_HISTORY_SQL = """
-- K1 Phase 0 initial records (v2.1 §7.1)
-- Execute once when paper trading starts.
INSERT INTO v2_strategy_phase_history (
    timestamp, strategy, from_phase, to_phase,
    rolling_W, rolling_R, sample_size,
    kelly_fraction_estimated, kelly_cap_lower, kelly_cap_upper,
    decision_id, created_at
) VALUES
    (NOW(), 'strategy_1', NULL, 'phase_0', NULL, NULL, 0, NULL, 0.20, 0.20, NULL, NOW()),
    (NOW(), 'strategy_2', NULL, 'phase_0', NULL, NULL, 0, NULL, 0.10, 0.10, NULL, NOW());
"""

DECISION_TRAIL_SQL = """
-- Paper startup decision_trail event (v2.0 §27)
-- Execute once after phase_history is inserted.
INSERT INTO v2_decision_trail (
    timestamp, decision_type, trigger_source, target_component,
    wiki_decision, decision_basis, created_at, created_by
) VALUES (
    NOW(),
    'paper_startup',
    'manual',
    'paper_engine',
    'paper trading 正式起動',
    'Wave 5 実装完了 + Helixa 実盘稳定 ≥ 3 月（確認待ち） + LOB collector 30 日充足',
    NOW(),
    'wiki'
);
"""


# ── Pre-flight checks ─────────────────────────────────────────────────────────

async def _check_bars(conn) -> dict[str, Any]:
    """Check v2_bars_4h row count."""
    try:
        row = await conn.fetchrow("SELECT COUNT(*) AS n FROM v2_bars_4h")
        count = int(row["n"])
        ok = count >= _MIN_BARS_30D
        return {"table": "v2_bars_4h", "count": count, "required": _MIN_BARS_30D, "ok": ok}
    except Exception as e:
        return {"table": "v2_bars_4h", "count": None, "required": _MIN_BARS_30D, "ok": False, "error": str(e)}


async def _check_strategy_params(conn) -> dict[str, Any]:
    """Check v2_strategy_params has required keys."""
    required_keys = ["h2_branching_threshold", "tda_l1_threshold", "sigma_window"]
    try:
        rows = await conn.fetch("SELECT param_key FROM v2_strategy_params")
        present = {r["param_key"] for r in rows}
        missing = [k for k in required_keys if k not in present]
        return {
            "table": "v2_strategy_params",
            "present": sorted(present),
            "missing": missing,
            "ok": len(missing) == 0,
        }
    except Exception as e:
        return {"table": "v2_strategy_params", "ok": False, "error": str(e)}


async def _check_lob_snapshots(conn) -> dict[str, Any]:
    """Check v2_lob_snapshots for LOB collector availability."""
    try:
        row = await conn.fetchrow("SELECT COUNT(*) AS n FROM v2_lob_snapshots")
        count = int(row["n"])
        return {"table": "v2_lob_snapshots", "count": count, "ok": count > 0}
    except Exception as e:
        return {"table": "v2_lob_snapshots", "count": None, "ok": False, "error": str(e)}


async def _check_helixa_derivatives(conn) -> dict[str, Any]:
    """Check helixa derivatives data availability (optional, may not be GRANTed)."""
    try:
        row = await conn.fetchrow(
            "SELECT COUNT(*) AS n FROM helixa.derivatives_snapshots LIMIT 1"
        )
        count = int(row["n"])
        return {"source": "helixa.derivatives_snapshots", "count": count, "ok": True}
    except Exception:
        return {"source": "helixa.derivatives_snapshots", "count": None, "ok": False,
                "note": "helixa GRANT not yet applied — expected"}


async def run_checks(db_url: str | None) -> list[dict[str, Any]]:
    """
    Run all pre-flight checks and return a list of result dicts.
    Returns mock results if db_url is None (dry-run mode).
    """
    if db_url is None:
        # Dry-run: return realistic stub results
        return [
            {"table": "v2_bars_4h", "count": 0, "required": _MIN_BARS_30D,
             "ok": False, "note": "dry-run — no DB connection"},
            {"table": "v2_strategy_params", "ok": False, "note": "dry-run"},
            {"table": "v2_lob_snapshots", "count": 0, "ok": False, "note": "dry-run"},
            {"source": "helixa.derivatives_snapshots", "ok": False,
             "note": "helixa GRANT not yet applied — expected"},
        ]

    try:
        import asyncpg
    except ImportError:
        logger.error("asyncpg not installed. Cannot connect to DB.")
        return []

    conn = await asyncpg.connect(db_url)
    try:
        results = await asyncio.gather(
            _check_bars(conn),
            _check_strategy_params(conn),
            _check_lob_snapshots(conn),
            _check_helixa_derivatives(conn),
        )
        return list(results)
    finally:
        await conn.close()


# ── Report generation ─────────────────────────────────────────────────────────

def _build_report(
    now: datetime,
    check_results: list[dict[str, Any]],
) -> str:
    date_str = now.strftime("%Y-%m-%d")
    lines = [
        f"# Selene Paper Startup Pre-flight Report — {date_str}\n",
        f"生成時刻: {now.strftime('%Y-%m-%d %H:%M UTC')}\n",
        "## 前提条件チェック\n",
        "| チェック項目 | 結果 | 詳細 |",
        "|---|---|---|",
    ]
    all_ok = True
    for r in check_results:
        label = r.get("table") or r.get("source", "?")
        ok = r.get("ok", False)
        status = "✅ OK" if ok else "❌ NG"
        if not ok:
            all_ok = False
        detail_parts = []
        if "count" in r:
            detail_parts.append(f"count={r['count']}")
        if "required" in r:
            detail_parts.append(f"required≥{r['required']}")
        if "missing" in r and r["missing"]:
            detail_parts.append(f"missing={r['missing']}")
        if "note" in r:
            detail_parts.append(r["note"])
        if "error" in r:
            detail_parts.append(f"error: {r['error']}")
        detail = ", ".join(detail_parts) if detail_parts else "-"
        lines.append(f"| {label} | {status} | {detail} |")

    lines += [
        "",
        "## 起動可否判断\n",
        f"{'✅ 全チェック通過 — Wiki 承認後に以下 SQL を実行してください。' if all_ok else '❌ 未充足条件あり — 起動を保留してください。'}",
        "",
        "## Phase History 初期化 SQL（実行前に Wiki + Claude 双重レビュー必須）\n",
        "```sql",
        PHASE_HISTORY_SQL.strip(),
        "```",
        "",
        "## Decision Trail 起動イベント SQL\n",
        "```sql",
        DECISION_TRAIL_SQL.strip(),
        "```",
        "",
        "## 注記\n",
        "- 上記 SQL は自動実行されません。Wiki が手動で実行してください。",
        "- 実行後 `v2_strategy_phase_history` と `v2_decision_trail` のレコードを確認すること。",
        "- Paper trading 起動後、Wave 6（K1 Phase 2 切換）は Month 3 評估まで禁止。",
    ]
    return "\n".join(lines) + "\n"


async def main(db_url: str | None, dry_run: bool) -> int:
    now = datetime.now(timezone.utc)
    logger.info("Paper startup pre-flight check (dry_run=%s)", dry_run)

    effective_url = None if dry_run else db_url
    check_results = await run_checks(effective_url)

    report_md = _build_report(now, check_results)

    # Write report
    os.makedirs(_REPORT_DIR, exist_ok=True)
    date_str = now.strftime("%Y-%m-%d")
    report_path = os.path.join(_REPORT_DIR, f"paper_startup_{date_str}.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report_md)
    logger.info("Startup report written to %s", report_path)

    # Print SQL (never auto-execute)
    print("\n" + "=" * 70)
    print("PHASE HISTORY SQL (Wiki must execute manually):")
    print(PHASE_HISTORY_SQL)
    print("DECISION TRAIL SQL (Wiki must execute manually):")
    print(DECISION_TRAIL_SQL)
    print("=" * 70 + "\n")

    failed = [r for r in check_results if not r.get("ok", False)
              if "helixa" not in str(r.get("source", ""))  # helixa expected to be NG
              and "helixa" not in str(r.get("note", ""))]
    if failed:
        logger.warning("%d required check(s) failed — do NOT start paper trading", len(failed))
        return 1
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Selene paper startup pre-flight")
    parser.add_argument("--db-url", default=None, help="asyncpg DB URL")
    parser.add_argument("--dry-run", action="store_true", help="Skip DB connection")
    args = parser.parse_args()

    rc = asyncio.run(main(db_url=args.db_url, dry_run=args.dry_run or args.db_url is None))
    sys.exit(rc)
