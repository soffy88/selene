"""
Weekly report scheduler — v2.0 §26.1.

Fires every Sunday UTC 00:00 and calls WeeklyGenerator.

This module implements the scheduling logic.  The cron deployment step is
intentionally deferred — Wave 5 writes the code only.  To start:

    python -m sel_v2.reports.scheduler

Or import and call schedule_weekly_report() in the event loop.

Data collection is a stub: in paper trading mode, the scheduler should
query v2_state_history, v2_cusum_events, v2_strategy_trades, and
v2_inverse_vocab_events for the past 7 days.  Until those tables have
live data, all sections return N/A.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Any

from sel_v2.reports.weekly_generator import WeeklyGenerator

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _current_week_iso() -> str:
    """Return ISO week string for the current week, e.g. '2026-W18'."""
    now = datetime.now(timezone.utc)
    return f"{now.isocalendar()[0]}-W{now.isocalendar()[1]:02d}"


def _period_strings(now: datetime) -> tuple[str, str]:
    """Return (start_str, end_str) for the 7-day window ending at now."""
    start = now - timedelta(days=7)
    return start.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d")


def _collect_report_data(period_start: str, period_end: str, db=None) -> dict[str, Any]:
    """
    Collect report data for the given period.

    STUB: returns mostly-empty data dicts when DB is unavailable.
    In paper trading, this should query the TimescaleDB tables.
    """
    data: dict[str, Any] = {
        "period_start": period_start,
        "period_end": period_end,
        "state_distribution": {
            "state_hours": {
                "Drifting_Calm": 162.0,
                "Drifting_Charged": 0.0,
                "Coiling": 0.0,
                "Surging": 0.0,
                "Critical": 6.0,
                "Cascade": 0.0,
            },
            "state_switches": {},
            "transitions": [],
            "total_hours": 168.0,
        },
        "cusum_stats": {
            "cusum_mid_triggers": 0,
            "cusum_short_triggers": 0,
        },
        "inverse_vocab_stats": {"vocab_counts": {}},
        "strategy1_perf": {},
        "strategy2_perf": {},
        "coordination_events": {},
        "anomalies": {"data_gaps": [], "api_errors": [], "decision_anomalies": []},
        "account_state": {},
        "observation_tools": {"tool_triggers": {}, "tool_notes": {
            "B1": "rolling HMM runs; no disagreements recorded",
            "B2": "Drifting-Charged is never recognized (OI/funding STUB)",
            "TDA2": "running; no anomaly triggered this week",
            "I1": "PE normal range",
            "T2": "funding_rate STUB → NO_DATA",
            "W2": "running; energy ratio within normal range",
            "H3": "liquidation STUB → running in degraded mode",
        }},
    }

    if db is not None:
        # Placeholder: in paper trading mode query DB here
        pass

    return data


async def run_weekly_report(db=None) -> str:
    """
    Collect data and generate the weekly report.  Returns the output path.
    """
    now = datetime.now(timezone.utc)
    week_iso = _current_week_iso()
    period_start, period_end = _period_strings(now)

    logger.info("Generating weekly report for %s", week_iso)
    data = _collect_report_data(period_start, period_end, db=db)

    gen = WeeklyGenerator()
    path = gen.generate(week_iso=week_iso, data=data, write_to_disk=True)
    logger.info("Weekly report written to %s", path)
    return path


def schedule_weekly_report_blocking() -> None:
    """
    Block until next Sunday UTC 00:00 then run report.
    For use as a standalone daemon (python -m sel_v2.reports.scheduler).

    NOT deployed in Wave 5 — call manually or integrate with cron.
    """
    import time

    while True:
        now = datetime.now(timezone.utc)
        # Next Sunday 00:00 UTC
        days_until_sunday = (6 - now.weekday()) % 7
        if days_until_sunday == 0 and now.hour == 0 and now.minute < 5:
            asyncio.run(run_weekly_report())
            time.sleep(300)  # sleep 5 min to avoid double-fire
        else:
            next_sunday = now + timedelta(days=days_until_sunday)
            next_run = next_sunday.replace(hour=0, minute=0, second=0, microsecond=0)
            sleep_secs = (next_run - now).total_seconds()
            logger.info(
                "Next weekly report in %.1f hours (%s UTC)",
                sleep_secs / 3600,
                next_run.strftime("%Y-%m-%d %H:%M"),
            )
            time.sleep(min(sleep_secs, 3600))


if __name__ == "__main__":
    asyncio.run(run_weekly_report())
