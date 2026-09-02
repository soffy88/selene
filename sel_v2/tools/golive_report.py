"""golive_report — GL1 T0.6 gate self-check CLI.

    python -m sel_v2.tools.golive_report --gate G0

G0 must be [NEEDS-HUMAN 5-minute confirm]'d (GL1 §3) before an epoch starts, and this
tool is what that confirmation reads — it must never overstate readiness. RED unless
every check genuinely passes; gaps are listed explicitly rather than rounded up to
"basically green" (repo-wide instruction: no softened verdicts).

Only --gate G0 is implemented (G1/G2 are Phase 1/2 — SEL2-SPEC-GL1 §4/§5 — and read
from the same infra this builds, but their specific checks aren't due yet).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import asyncpg

from sel_v2.tools import epoch as epoch_mod

# "满格" (window full) operational definition for the None-prone fields GL1 T0.6 names.
# Not itself specified numerically in the spec — chosen conservatively (near-total
# coverage, not "mostly"). GL1 explicitly frames the underlying gap as a calendar gate
# (30-day epoch, D3), so RED here pre-epoch is expected, not a bug in this tool.
_FILL_RATE_GREEN_THRESHOLD = 0.95
_RECENT_DAYS = 30
_STATE_LABELS = (
    "Coiling",
    "Surging",
    "Drifting_Calm",
    "Drifting_Charged",
    "Critical",
    "Cascade",
)


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str
    color: str = field(init=False)

    def __post_init__(self):
        self.color = "GREEN" if self.ok else "RED"


@dataclass
class GateReport:
    gate: str
    generated_at: datetime
    checks: list
    overall: str

    def render(self) -> str:
        lines = [
            f"golive_report --gate {self.gate}  ({self.generated_at.isoformat()})",
            "",
        ]
        for c in self.checks:
            lines.append(f"[{c.color}] {c.name}: {c.detail}")
        lines.append("")
        lines.append(f"OVERALL: {self.overall}")
        return "\n".join(lines)


async def _none_prone_fill_rates(pool: asyncpg.Pool, since: datetime) -> dict[str, float]:
    """Reads the GL1 T0.3 decision_trail JSONB directly — it's the audit trail of what
    the engine actually used, so it's also the fill-rate source of truth (no second,
    possibly-divergent recompute)."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT decision_trail FROM v2_strategy_decision "
            "WHERE strategy='strategy_1' AND timestamp >= $1 AND decision_trail IS NOT NULL "
            "ORDER BY timestamp ASC",
            since,
        )
    fields = ("entropy_variance_rising", "oi_change_rate", "funding_persistent")
    if not rows:
        return {f: 0.0 for f in fields}
    counts = {f: 0 for f in fields}
    for r in rows:
        trail = r["decision_trail"]
        if isinstance(trail, str):
            trail = json.loads(trail)
        for f in fields:
            if trail.get(f) is not None:
                counts[f] += 1
    n = len(rows)
    return {f: counts[f] / n for f in fields}


async def _state_distribution(pool: asyncpg.Pool, since: datetime) -> dict[str, int]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT state, count(*) AS n FROM v2_state_history WHERE timestamp >= $1 GROUP BY state",
            since,
        )
    return {r["state"]: r["n"] for r in rows}


async def _decision_trail_liveness(pool: asyncpg.Pool, since: datetime) -> tuple[int, int]:
    """P2-1 (T0.3/D1) status: is decision_trail actually being populated, i.e. is the
    engine writing what it used, not leaving the column perpetually NULL."""
    async with pool.acquire() as conn:
        total = await conn.fetchval("SELECT count(*) FROM v2_strategy_decision WHERE timestamp >= $1", since)
        populated = await conn.fetchval(
            "SELECT count(*) FROM v2_strategy_decision WHERE timestamp >= $1 AND decision_trail IS NOT NULL",
            since,
        )
    return populated or 0, total or 0


async def _staleness_summary(pool: asyncpg.Pool, since: datetime) -> tuple[list, list]:
    """Returns (currently_stale_sources, recent_transition_rows)."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT DISTINCT ON (source) source, stale, reason_code, detected_at "
            "FROM v2_staleness_events ORDER BY source, detected_at DESC"
        )
        recent = await conn.fetch(
            "SELECT source, stale, reason_code, detected_at FROM v2_staleness_events "
            "WHERE detected_at >= $1 ORDER BY detected_at DESC",
            since,
        )
    currently_stale = [r["source"] for r in rows if r["stale"]]
    return currently_stale, [dict(r) for r in recent]


async def build_g0_report(pool: asyncpg.Pool) -> GateReport:
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=_RECENT_DAYS)
    checks: list[CheckResult] = []

    # 1. epoch status
    est = await epoch_mod.status(pool)
    epoch_ok = est.status == "CLEAN"
    checks.append(
        CheckResult(
            "epoch",
            epoch_ok,
            f"status={est.status} epoch_id={est.epoch_id} started_at={est.started_at} "
            + (
                ""
                if est.status != "DIRTY"
                else "(fingerprint drifted since epoch start — states/**/strategies/** changed)"
            ),
        )
    )

    # 2. None-prone field fill rates (last _RECENT_DAYS, epoch-agnostic when no epoch yet —
    # golive_report's own job is to report the gap, not require an epoch to run at all).
    window_start = est.started_at if est.status == "CLEAN" and est.started_at else since
    fill_rates = await _none_prone_fill_rates(pool, window_start)
    for field_name, rate in fill_rates.items():
        ok = rate >= _FILL_RATE_GREEN_THRESHOLD
        checks.append(
            CheckResult(
                f"fill_rate:{field_name}",
                ok,
                f"{rate:.1%} non-None (threshold {_FILL_RATE_GREEN_THRESHOLD:.0%}) "
                f"since {window_start.isoformat() if window_start else 'N/A'}",
            )
        )

    # 3. state distribution (informational — no pass/fail, just visibility)
    dist = await _state_distribution(pool, window_start)
    total_bars = sum(dist.values())
    dist_detail = ", ".join(f"{s}={dist.get(s, 0)}" for s in _STATE_LABELS) if total_bars else "no bars in window"
    checks.append(CheckResult("state_distribution", True, dist_detail))

    # 4. Cascade/Critical trigger counts (informational)
    cascade_n = dist.get("Cascade", 0)
    critical_n = dist.get("Critical", 0)
    checks.append(
        CheckResult(
            "cascade_critical_counts",
            True,
            f"Cascade={cascade_n} Critical={critical_n} (of {total_bars} state records in window)",
        )
    )

    # 5. P2-1 (T0.3/D1) decision_trail liveness
    populated, total = await _decision_trail_liveness(pool, window_start)
    p21_ok = total > 0 and populated == total
    checks.append(
        CheckResult(
            "P2-1_decision_trail",
            p21_ok,
            f"{populated}/{total} v2_strategy_decision rows have decision_trail populated"
            if total
            else "no v2_strategy_decision rows in window",
        )
    )

    # 6. staleness (informational transitions + a gating check: nothing stale right now)
    currently_stale, recent_events = await _staleness_summary(pool, since)
    checks.append(
        CheckResult(
            "staleness_current",
            not currently_stale,
            f"currently stale: {currently_stale}" if currently_stale else "all sources fresh",
        )
    )
    checks.append(
        CheckResult(
            "staleness_events_30d",
            True,
            f"{len(recent_events)} transition(s) in the last {_RECENT_DAYS}d",
        )
    )

    overall = "GREEN" if all(c.ok for c in checks) else "RED"
    return GateReport(gate="G0", generated_at=now, checks=checks, overall=overall)


async def _main() -> None:
    parser = argparse.ArgumentParser(description="sel_v2 golive_report (GL1 T0.6)")
    parser.add_argument("--gate", choices=["G0", "G1", "G2"], default="G0")
    args = parser.parse_args()

    dsn = os.environ["DB_URL"].replace("postgresql+asyncpg://", "postgresql://")
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    try:
        if args.gate != "G0":
            print(f"--gate {args.gate} is not implemented yet (Phase 1/2, not due — GL1 §4/§5).")
            return
        report = await build_g0_report(pool)
        print(report.render())
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(_main())
