import argparse
import asyncio
import logging
import os
from datetime import datetime, timezone

import asyncpg

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("paper_startup")

# ── Provisioning SQL (module-level so it can be embedded in the startup report) ──

PHASE_HISTORY_SQL = """
INSERT INTO v2_strategy_phase_history (
    timestamp, strategy, from_phase, to_phase,
    rolling_w, rolling_r, sample_size,
    kelly_fraction_estimated, kelly_cap_lower, kelly_cap_upper,
    decision_id, created_at
) VALUES
    (NOW(), 'strategy_1', NULL, 'phase_0', NULL, NULL, 0, NULL, 0.20, 0.20, NULL, NOW()),
    (NOW(), 'strategy_2', NULL, 'phase_0', NULL, NULL, 0, NULL, 0.10, 0.10, NULL, NOW())
ON CONFLICT DO NOTHING;
"""

DECISION_TRAIL_SQL = """
INSERT INTO v2_decision_trail (
    timestamp, decision_type, trigger_source, target_component,
    wiki_decision, decision_basis, created_at, created_by
) VALUES (
    NOW(), 'paper_startup', 'manual', 'paper_engine',
    'paper trading 正式启动',
    'Wave 1-5 重建完成 + WSL 崩溃后全新部署',
    NOW(), 'wiki'
);
"""

# Each check is (label, table, source, required-condition description).
_CHECKS = [
    {"label": "v2_bars_4h 行数 >= 180", "table": "v2_bars_4h", "source": "platform-postgres"},
    {"label": "v2_strategy_params 必填参数", "table": "v2_strategy_params", "source": "platform-postgres"},
    {"label": "v2_lob_snapshots 行数 > 0", "table": "v2_lob_snapshots", "source": "platform-postgres"},
    {"label": "helixa GRANT 可访问", "table": None, "source": "helixa"},
]


async def run_checks(db_url=None):
    """Run pre-flight checks for paper trading.

    When ``db_url`` is ``None`` this is a dry-run: every check returns ``ok=False``
    with an explanatory detail (no DB is contacted). Returns a list of dicts, one
    per check, each with keys ``label``/``table``/``source``/``ok``/``detail``.
    """
    if db_url is None:
        results = []
        for c in _CHECKS:
            results.append({**c, "ok": False, "detail": "dry-run: 未连接数据库"})
        return results

    results = []
    pool = await asyncpg.create_pool(db_url)
    try:
        async with pool.acquire() as conn:
            # 1. v2_bars_4h count >= 180
            try:
                bar_count = await conn.fetchval("SELECT count(*) FROM v2_bars_4h")
                results.append(
                    {
                        **_CHECKS[0],
                        "ok": bar_count >= 180,
                        "detail": f"count={bar_count}",
                    }
                )
            except Exception as e:  # noqa: BLE001
                results.append({**_CHECKS[0], "ok": False, "detail": f"query failed: {e}"})

            # 2. v2_strategy_params required keys
            required = {"h2_branching_threshold", "cusum_mid_quantile"}
            try:
                params = await conn.fetch("SELECT param_key FROM v2_strategy_params")
                param_keys = {p["param_key"] for p in params}
                missing = required - param_keys
                results.append(
                    {
                        **_CHECKS[1],
                        "ok": not missing,
                        "detail": f"loaded={len(param_keys)}" + (f", missing={missing}" if missing else ""),
                    }
                )
            except Exception as e:  # noqa: BLE001
                results.append({**_CHECKS[1], "ok": False, "detail": f"query failed: {e}"})

            # 3. v2_lob_snapshots count > 0
            try:
                lob_count = await conn.fetchval("SELECT count(*) FROM v2_lob_snapshots")
                results.append(
                    {
                        **_CHECKS[2],
                        "ok": lob_count > 0,
                        "detail": f"count={lob_count}",
                    }
                )
            except Exception as e:  # noqa: BLE001
                results.append({**_CHECKS[2], "ok": False, "detail": f"query failed: {e}"})

            # 4. helixa GRANT (non-blocking warning)
            try:
                helixa_exists = await conn.fetchval(
                    "SELECT 1 FROM information_schema.tables WHERE table_name = 'metadata' AND table_catalog = 'helixa'"
                )
                results.append(
                    {
                        **_CHECKS[3],
                        "ok": bool(helixa_exists),
                        "detail": "accessible" if helixa_exists else "not found (non-blocking)",
                    }
                )
            except Exception as e:  # noqa: BLE001
                results.append({**_CHECKS[3], "ok": False, "detail": f"check failed: {e} (non-blocking)"})
    finally:
        await pool.close()

    return results


def _build_report(now, results):
    """Render a markdown pre-flight report from ``run_checks`` results."""
    date_str = now.strftime("%Y-%m-%d")
    lines = [
        f"# Paper Trading 启动前提条件チェック ({date_str})",
        "",
        "## 前提条件チェック",
        "",
        "| 检查项 | 数据源 | 状态 | 详情 |",
        "|---|---|---|---|",
    ]
    for r in results:
        status = "✅ OK" if r.get("ok") else "❌ NG"
        table = r.get("table") or r.get("source") or "-"
        lines.append(f"| {r.get('label', '')} | {table} | {status} | {r.get('detail', '')} |")

    lines += [
        "",
        "## Provisioning SQL（auto-exec 时执行）",
        "",
        "```sql",
        PHASE_HISTORY_SQL.strip(),
        "",
        DECISION_TRAIL_SQL.strip(),
        "```",
    ]
    return "\n".join(lines)


async def provision(db_url):
    pool = await asyncpg.create_pool(db_url)
    try:
        async with pool.acquire() as conn:
            await conn.execute(PHASE_HISTORY_SQL)
            await conn.execute(DECISION_TRAIL_SQL)
            logger.info("Provisioning SQL executed successfully")
    finally:
        await pool.close()


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-url", default=os.getenv("DB_URL"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--auto-exec", action="store_true")
    args = parser.parse_args()

    db_url = None if args.dry_run else args.db_url
    results = await run_checks(db_url=db_url)
    report = _build_report(datetime.now(timezone.utc), results)
    print(report)

    if args.dry_run:
        return

    if all(r["ok"] for r in results if r["source"] != "helixa"):
        logger.info("Pre-checks PASSED")
        if args.auto_exec:
            await provision(args.db_url)
    else:
        logger.error("Pre-checks FAILED")
        exit(1)


if __name__ == "__main__":
    asyncio.run(main())
