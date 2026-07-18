"""Weekly live capture-rate monitor (Wave V22-D, offline follow-up to V22-C).

V22-C's historical capture rate (29-39%, `sel_v2/reports/trend_leg_census_v1.md`)
is confounded by feature degradation (81.6% of the 2yr history predates the OI/
funding/entropy/LOB feeds — see `state_annotation_baseline_v1.md`) and can't be
extrapolated to what the *current*, feature-complete deployment actually captures.
This module answers that prospectively: every week, run the SAME three coarse
zigzag thresholds as `leg_census.py` (unmodified, frozen per the Wave's red line)
over the most recent 90 days of `v2_bars_4h`, and compare against `v2_state_history`
(the live engine's own record) restricted to the live-monitored domain.

Domain note: `v2_state_history` was backfilled with the full 2yr history during the
2026-06-30/07-01 SEL live-ops round (commit `bf80c6e`), so a bar merely *having a
row* no longer distinguishes genuinely-live-recorded data from that backfill — every
bar has a row now. The live-monitored domain is therefore the explicit cutoff the
Wave gave (2026-06-15, when the current-code engine's forward recording is
considered to start), not row presence. A leg counts "in domain" when >=50% of its
bars are at/after that cutoff — same 50% convention as the Surging-capture check.

Writes `v2_capture_rate_weekly` (append-only, one row per threshold per run) and
appends a section to `sel_v2/reports/capture_rate_weekly.md`. Read+append only —
never touches strategies/**, states/**, v2_state_annotation, or the epoch.

Run once now:      python -m sel_v2.offline.capture_monitor
Run forever (weekly, Monday 00:30 UTC), used by the v2-capture-monitor service:
                   python -m sel_v2.offline.capture_monitor --serve
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncpg
import numpy as np

from sel_v2.offline.leg_census import (
    CAPTURE_OVERLAP_THRESHOLD,
    CONFIGS,
    _is_wiki_spec,
    census,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("capture_monitor")

SYMBOL = os.environ.get("SYMBOLS", "BTC-USDT")
REPORT_PATH = Path(__file__).resolve().parents[1] / "reports" / "capture_rate_weekly.md"

WINDOW_DAYS = 90
DOMAIN_START = datetime(2026, 6, 15, tzinfo=timezone.utc)
DOMAIN_OVERLAP_THRESHOLD = 0.50

RUN_WEEKDAY = 0  # Monday
RUN_HOUR = 0
RUN_MINUTE = 30


async def _load(pool, window_days: int = WINDOW_DAYS):
    bar_rows = await pool.fetch(
        "SELECT time, high, low, close FROM v2_bars_4h WHERE symbol=$1 "
        "AND time >= (SELECT max(time) FROM v2_bars_4h WHERE symbol=$1) - make_interval(days => $2) "
        "ORDER BY time ASC",
        SYMBOL,
        window_days,
    )
    state_rows = await pool.fetch(
        "SELECT timestamp, state FROM v2_state_history ORDER BY timestamp ASC"
    )
    state_by_ts = {r["timestamp"]: r["state"] for r in state_rows}
    times, high, low, close, states = [], [], [], [], []
    for r in bar_rows:
        st = state_by_ts.get(r["time"])
        if st is None:
            continue
        times.append(r["time"])
        high.append(float(r["high"]))
        low.append(float(r["low"]))
        close.append(float(r["close"]))
        states.append(st)
    return times, np.array(high), np.array(low), np.array(close), states


def _domain_fraction(times, start_idx: int, end_idx: int) -> float:
    n = end_idx - start_idx + 1
    in_domain = sum(
        1 for i in range(start_idx, end_idx + 1) if times[i] >= DOMAIN_START
    )
    return in_domain / n


def _modal_state(states, start_idx: int, end_idx: int) -> str:
    return Counter(states[start_idx : end_idx + 1]).most_common(1)[0][0]


def run_one_config(name: str, cfg, high, low, close, states, times) -> dict:
    legs, _tail_bars = census(high, low, close, states, cfg)
    legs_total = len(legs)
    wiki_legs = [l for l in legs if _is_wiki_spec(l)]
    in_domain = [
        l
        for l in wiki_legs
        if _domain_fraction(times, l.start_idx, l.end_idx) >= DOMAIN_OVERLAP_THRESHOLD
    ]
    captured = [l for l in in_domain if l.surging_overlap >= CAPTURE_OVERLAP_THRESHOLD]
    missed = [l for l in in_domain if l.surging_overlap < CAPTURE_OVERLAP_THRESHOLD]
    capture_rate = len(captured) / len(in_domain) if in_domain else None

    missed_detail = [
        {
            "start": times[l.start_idx].isoformat(),
            "end": times[l.end_idx].isoformat(),
            "direction": "long" if l.direction == 1 else "short",
            "duration_days": round(l.duration_days, 2),
            "push_count": l.push_count,
            "modal_state": _modal_state(states, l.start_idx, l.end_idx),
        }
        for l in missed
    ]
    return {
        "threshold": name,
        "legs_total": legs_total,
        "legs_spec": len(wiki_legs),
        "legs_in_domain": len(in_domain),
        "captured": len(captured),
        "capture_rate": capture_rate,
        "missed_detail": missed_detail,
    }


async def _write_row(pool, run_at, window_start, window_end, result: dict) -> None:
    await pool.execute(
        """
        INSERT INTO v2_capture_rate_weekly
            (run_at, threshold, window_start, window_end, legs_total, legs_spec,
             legs_in_domain, captured, capture_rate, missed_detail)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb)
        """,
        run_at,
        result["threshold"],
        window_start,
        window_end,
        result["legs_total"],
        result["legs_spec"],
        result["legs_in_domain"],
        result["captured"],
        result["capture_rate"],
        json.dumps(result["missed_detail"]),
    )


def _pct(x) -> str:
    return f"{x * 100:.1f}%" if x is not None else "n/a"


def _build_section(run_at, window_start, window_end, results: list[dict]) -> str:
    lines = [
        f"## Run {run_at.isoformat()}",
        "",
        f"Window: {window_start.date()} -> {window_end.date()} "
        f"({(window_end - window_start).days} days). Domain cutoff: {DOMAIN_START.date()}.",
        "",
        "| threshold | legs (window) | Wiki-spec legs | in domain | captured | capture rate |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for r in results:
        lines.append(
            f"| {r['threshold']} | {r['legs_total']} | {r['legs_spec']} | "
            f"{r['legs_in_domain']} | {r['captured']} | {_pct(r['capture_rate'])} |"
        )
    lines.append("")
    for r in results:
        lines.append(f"### {r['threshold']} — missed legs ({len(r['missed_detail'])})")
        lines.append("")
        if r["missed_detail"]:
            lines.append(
                "| start | end | direction | duration (d) | push | modal annotation |"
            )
            lines.append("|---|---|---|---:|---:|---|")
            for m in r["missed_detail"]:
                lines.append(
                    f"| {m['start']} | {m['end']} | {m['direction']} | "
                    f"{m['duration_days']} | {m['push_count']} | {m['modal_state']} |"
                )
        else:
            lines.append("- none")
        lines.append("")
    return "\n".join(lines)


async def run_once() -> list[dict]:
    dsn = os.environ["DB_URL"].replace("postgresql+asyncpg://", "postgresql://")
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=3)
    try:
        times, high, low, close, states = await _load(pool)
        if not times:
            raise RuntimeError(
                "v2_bars_4h / v2_state_history unreadable or empty — STOP per Wave red line"
            )
        run_at = datetime.now(timezone.utc)
        window_start, window_end = times[0], times[-1]
        logger.info(
            "loaded %d bars, window %s -> %s", len(close), window_start, window_end
        )

        results = [
            run_one_config(name, cfg, high, low, close, states, times)
            for name, cfg in CONFIGS
        ]
        for r in results:
            logger.info(
                "%s: legs_total=%d legs_spec=%d in_domain=%d captured=%d capture_rate=%s",
                r["threshold"],
                r["legs_total"],
                r["legs_spec"],
                r["legs_in_domain"],
                r["captured"],
                _pct(r["capture_rate"]),
            )
            await _write_row(pool, run_at, window_start, window_end, r)

        section = _build_section(run_at, window_start, window_end, results)
        print(section)
        try:
            existing = (
                REPORT_PATH.read_text()
                if REPORT_PATH.exists()
                else (
                    "# Weekly Live Capture-Rate Monitor (Wave V22-D)\n\n"
                    "Prospective, feature-complete-era capture-rate tracking — follow-up to "
                    "`trend_leg_census_v1.md` (V22-C), whose historical rate is confounded by "
                    "feature degradation. Appends one section per run.\n"
                )
            )
            REPORT_PATH.write_text(existing.rstrip("\n") + "\n\n" + section + "\n")
            logger.info("report appended: %s", REPORT_PATH)
        except OSError as exc:
            logger.warning("could not write report file (%s): %s", REPORT_PATH, exc)

        return results
    finally:
        await pool.close()


def _seconds_until_next_run(now: datetime) -> float:
    days_ahead = (RUN_WEEKDAY - now.weekday()) % 7
    candidate = (now + timedelta(days=days_ahead)).replace(
        hour=RUN_HOUR, minute=RUN_MINUTE, second=0, microsecond=0
    )
    if candidate <= now:
        candidate += timedelta(days=7)
    return (candidate - now).total_seconds()


async def serve_forever() -> None:
    """Sleep until the next Monday 00:30 UTC, run once, log ERROR (not crash) on
    failure, repeat forever. Matches services/healthcheck/main.py's own
    never-crash-the-loop convention."""
    logger.info(
        "capture_monitor scheduler started (weekly, Monday %02d:%02d UTC)",
        RUN_HOUR,
        RUN_MINUTE,
    )
    while True:
        now = datetime.now(timezone.utc)
        delay = _seconds_until_next_run(now)
        logger.info(
            "next run in %.1f hours (at %s)",
            delay / 3600,
            now + timedelta(seconds=delay),
        )
        await asyncio.sleep(delay)
        try:
            await run_once()
        except Exception as exc:  # noqa: BLE001 — never let a bad week kill the scheduler
            logger.error("weekly capture check failed: %s", exc)
        await asyncio.sleep(60)  # clear the run window before recomputing next-run


if __name__ == "__main__":
    if "--serve" in sys.argv:
        asyncio.run(serve_forever())
    else:
        asyncio.run(run_once())
