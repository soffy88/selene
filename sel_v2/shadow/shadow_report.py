"""Weekly shadow-execution report (Wave EXEC-S, Part 3).

Summarises `v2_shadow_orders` into `sel_v2/reports/shadow_exec_weekly.md`: how many
signals have accumulated toward the D4 gate (N>=30), how often the limit arm
actually filled, and — the number the gate turns on — the cumulative outcome
difference between the two arms, split by Type.

Kept as its own file rather than a section inside `capture_rate_weekly.md`: that
report belongs to Wave V22-D, appends its own per-run sections, and is read on a
different question. Interleaving two Waves' metrics would make both harder to read.

Read-only over v2_shadow_orders; writes one markdown file. Never touches
strategies/**, states/**, a live table, or the epoch.

Run once:  python -m sel_v2.shadow.shadow_report
Weekly (Mon 02:30 UTC, after fill_prob's 01:30 slot):
           python -m sel_v2.shadow.shadow_report --serve
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncpg

logger = logging.getLogger(__name__)

REPORT = Path(__file__).resolve().parents[1] / "reports" / "shadow_exec_weekly.md"
GATE_N = 30  # D4: at least this many closed signals before the gate can be read

RUN_WEEKDAY, RUN_HOUR, RUN_MINUTE = 0, 2, 30

HEADER = """# Weekly Shadow-Execution Report (Wave EXEC-S)

Dual-arm record of what a limit order *would* have done beside the market order
actually taken, for every S2 would-enter signal. Feeds the D4 gate: at least
**N>=30** closed signals AND a positive cumulative limit-arm advantage.

Shadow only — no order was ever placed. Appends one section per run.
"""


def _dsn() -> str:
    return os.environ["DB_URL"].replace("postgresql+asyncpg://", "postgresql://")


def _fmt(x, nd: int = 6) -> str:
    return "n/a" if x is None else f"{float(x):.{nd}f}"


async def build_section(conn: asyncpg.Connection) -> str:
    total = await conn.fetchval("SELECT count(*) FROM v2_shadow_orders")
    closed = await conn.fetchval(
        "SELECT count(*) FROM v2_shadow_orders WHERE outcome_limit IS NOT NULL"
    )
    rows = await conn.fetch(
        """
        SELECT COALESCE(entry_type,'?') AS t,
               count(*)                                        AS n,
               count(*) FILTER (WHERE limit_status='FILLED')    AS filled,
               count(*) FILTER (WHERE limit_status='TIMEOUT_CANCELLED') AS cancelled,
               count(*) FILTER (WHERE limit_status='TIMEOUT_MARKET')    AS crossed,
               sum(outcome_limit)                              AS sum_limit,
               sum(outcome_market)                             AS sum_market
        FROM v2_shadow_orders
        WHERE outcome_limit IS NOT NULL
        GROUP BY 1 ORDER BY 1
        """
    )
    tot = await conn.fetchrow(
        "SELECT sum(outcome_limit) AS l, sum(outcome_market) AS m "
        "FROM v2_shadow_orders WHERE outcome_limit IS NOT NULL"
    )
    resting = await conn.fetchval(
        "SELECT count(*) FROM v2_shadow_orders WHERE limit_status='RESTING'"
    )

    # Wave S2G Part 4: the S2 event funnel sits alongside the shadow numbers
    # because they answer the same question from two ends — how many chances the
    # strategy saw, and what the limit arm would have done with them.
    s2 = await conn.fetchrow(
        """
        SELECT count(DISTINCT event_id) AS events,
               count(*) FILTER (WHERE action IN ('ENTER_LONG','ENTER_SHORT')) AS entries,
               count(*) FILTER (WHERE reason ILIKE '%THROTTLED%') AS throttled
        FROM v2_strategy_decision
        WHERE strategy='strategy_2' AND event_id IS NOT NULL
          AND timestamp > now() - interval '7 days'
        """
    )

    now = datetime.now(timezone.utc)
    lines = [f"\n## Run {now.isoformat()}\n"]
    lines.append(
        f"\n### S2 event funnel (7d)\n\n"
        f"events **{s2['events'] or 0}** | entries **{s2['entries'] or 0}** | "
        f"throttled **{s2['throttled'] or 0}**"
        f" — baseline ~40 events/day (531 confirmed over 13.1d; the ~64/day figure\n"
        f"in S2G-0 counted clusters including singletons, which never confirm);\n"
        f" entries are capped at 4/UTC day.\n"
    )
    lines.append(
        f"Signals recorded: **{total}** | closed (24h mark in): **{closed}** | "
        f"still resting: {resting}\n"
    )
    lines.append(
        f"D4 progress: **{closed}/{GATE_N}** "
        f"({'gate readable' if closed >= GATE_N else 'accumulating'})\n"
    )

    if not rows:
        lines.append(
            "\nNo closed signals yet — nothing to compare. This is the expected "
            "state while S2 produces no would-enter signals; the recorder is in "
            "place and waiting, which is not a failure.\n"
        )
        return "".join(lines)

    lines.append(
        "\n| type | n | filled | cancelled | crossed | fill rate | Σ limit | Σ market | Σ diff |"
    )
    lines.append("\n|---|---:|---:|---:|---:|---:|---:|---:|---:|\n")
    for r in rows:
        n = r["n"] or 0
        fr = f"{(r['filled'] or 0) / n:.1%}" if n else "n/a"
        diff = (
            None
            if r["sum_limit"] is None or r["sum_market"] is None
            else float(r["sum_limit"]) - float(r["sum_market"])
        )
        lines.append(
            f"| {r['t']} | {n} | {r['filled']} | {r['cancelled']} | {r['crossed']} | "
            f"{fr} | {_fmt(r['sum_limit'])} | {_fmt(r['sum_market'])} | {_fmt(diff)} |\n"
        )

    net = (
        None
        if tot["l"] is None or tot["m"] is None
        else float(tot["l"]) - float(tot["m"])
    )
    verdict = "n/a"
    if net is not None:
        if closed < GATE_N:
            verdict = f"below N={GATE_N}, not yet readable"
        else:
            verdict = "limit arm ahead" if net > 0 else "limit arm NOT ahead"
    lines.append(f"\n**Net limit-minus-market: {_fmt(net)}** — {verdict}\n")
    return "".join(lines)


async def run_once() -> str:
    conn = await asyncpg.connect(_dsn())
    try:
        section = await build_section(conn)
    finally:
        await conn.close()
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    if not REPORT.exists():
        REPORT.write_text(HEADER)
    with REPORT.open("a") as fh:
        fh.write(section)
    logger.info("shadow report appended to %s", REPORT)
    return section


def _seconds_until_next_run(now: datetime) -> float:
    days = (RUN_WEEKDAY - now.weekday()) % 7
    target = (now + timedelta(days=days)).replace(
        hour=RUN_HOUR, minute=RUN_MINUTE, second=0, microsecond=0
    )
    if target <= now:
        target += timedelta(days=7)
    return (target - now).total_seconds()


async def serve_forever() -> None:
    logger.info(
        "shadow_report scheduler started (weekly, Mon %02d:%02d UTC)",
        RUN_HOUR,
        RUN_MINUTE,
    )
    while True:
        await asyncio.sleep(_seconds_until_next_run(datetime.now(timezone.utc)))
        try:
            await run_once()
        except Exception as exc:  # noqa: BLE001 — a bad week must not kill the loop
            logger.error("weekly shadow report failed: %s", exc)
        await asyncio.sleep(60)


if __name__ == "__main__":
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    if "--serve" in sys.argv:
        asyncio.run(serve_forever())
    else:
        asyncio.run(run_once())
