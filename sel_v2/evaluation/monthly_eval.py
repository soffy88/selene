"""Monthly observation-tool evaluation service (2026-07-12, optimization A3).

ToolEvaluator was manual-only ("Usage (manual, monthly)") — with 12 tools now
live-collecting vocab events, the monthly cadence should not depend on someone
remembering. Mirrors the capture_monitor service pattern: sleep until the 1st
of each month 01:00 UTC, run the full evaluation (per-tool metrics + pairwise
correlations), persist to v2_tool_evaluation_results (the evaluator does this
itself), and drop a one-row summary into v2_decision_trail
(decision_type='tool_eval_monthly') so the run is durably visible even though
the container filesystem is ephemeral.

This is the rolling monthly baseline; the FORMAL Month-3 三选一 evaluation
(pool discipline) is still a human-triggered run with phase='month_3'.

Run once now:  python -m sel_v2.evaluation.monthly_eval
Run forever:   python -m sel_v2.evaluation.monthly_eval --serve

Observation/evaluation layer only — touches nothing under strategies/**,
states/**, or the epoch (DBWriter is imported, never modified).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sel_v2.evaluation.tool_evaluator import ToolEvaluator
from sel_v2.strategies.db_writer import DBWriter

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("monthly_eval")

REPORT_PATH = Path(__file__).resolve().parents[1] / "reports" / "tool_evaluation_monthly.md"
RUN_HOUR_UTC = 1  # 1st of month, 01:00 UTC
LOOKBACK_DAYS = 90


async def run_once() -> list[dict]:
    now = datetime.now(timezone.utc)
    phase = f"monthly-{now:%Y%m}"
    writer = DBWriter()
    await writer.connect()
    try:
        evaluator = ToolEvaluator(writer)
        results = await evaluator.evaluate_all(phase=phase, lookback_days=LOOKBACK_DAYS)
        try:
            corr = await evaluator.compute_correlations(phase=phase, lookback_days=LOOKBACK_DAYS)
        except Exception as exc:  # noqa: BLE001 — correlations are best-effort
            logger.warning("correlations failed: %s", exc)
            corr = {}

        decided = {r["tool_id"]: (r["decision"], r["sample_size"]) for r in results}
        logger.info("evaluated %d tools: %s", len(results), decided)

        # Durable one-row summary (container FS is ephemeral; DB is not).
        try:
            await writer._conn.execute(
                """
                INSERT INTO v2_decision_trail
                    (timestamp, decision_type, trigger_source, target_component,
                     decision_basis, wiki_decision, created_by, tool_evaluation)
                VALUES ($1, 'tool_eval_monthly', 'monthly_eval_service',
                        'observation_tools', $2, 'AUTO', 'monthly_eval', $3::jsonb)
                """,
                now,
                f"{phase}: {len(results)} tools, lookback {LOOKBACK_DAYS}d",
                json.dumps({"decisions": decided, "correlations": corr}, default=str),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("decision_trail summary failed: %s", exc)

        # Best-effort markdown append (useful for manual host runs).
        lines = [
            f"\n## {phase} ({now:%Y-%m-%d %H:%M} UTC, lookback {LOOKBACK_DAYS}d)\n",
            "| tool | decision | n | reason |",
            "|---|---|---:|---|",
        ]
        for r in results:
            lines.append(
                f"| {r['tool_id']} {r['tool_name']} | {r['decision']} | {r['sample_size']} | {r['decision_reason']} |"
            )
        try:
            with REPORT_PATH.open("a") as f:
                f.write("\n".join(lines) + "\n")
            logger.info("report appended: %s", REPORT_PATH)
        except OSError as exc:
            logger.warning("report append failed (%s): %s", REPORT_PATH, exc)
        return results
    finally:
        await writer.close()


def _seconds_until_next_run(now: datetime) -> float:
    nxt = now.replace(day=1, hour=RUN_HOUR_UTC, minute=0, second=0, microsecond=0) + timedelta(days=32)
    nxt = nxt.replace(day=1)
    return (nxt - now).total_seconds()


async def serve_forever() -> None:
    while True:
        now = datetime.now(timezone.utc)
        delay = _seconds_until_next_run(now)
        logger.info(
            "next monthly evaluation in %.1f days (at %s)",
            delay / 86400,
            now + timedelta(seconds=delay),
        )
        await asyncio.sleep(delay)
        try:
            await run_once()
        except Exception as exc:  # noqa: BLE001 — never let one month kill the loop
            logger.error("monthly evaluation failed: %s", exc)
        await asyncio.sleep(60)


if __name__ == "__main__":
    if "--serve" in sys.argv:
        asyncio.run(serve_forever())
    else:
        asyncio.run(run_once())
