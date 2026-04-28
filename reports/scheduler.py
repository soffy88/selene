"""
ReportScheduler — Wave 5.

Called each bar; triggers WeeklyReportGenerator on ISO week boundary
(Monday 00:00 UTC).
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from paper_trading.runner import PaperTradingRunner
from paper_trading.trail import DecisionTrail
from reports.generator import WeeklyReportGenerator
from sel_engine.states.health import HealthMonitor


class ReportScheduler:
    """Called each bar. Triggers report generation on ISO week boundary."""

    def __init__(
        self,
        runner: PaperTradingRunner,
        output_dir: str = "reports/weekly",
    ) -> None:
        self._runner = runner
        self._output_dir = output_dir
        self._generator = WeeklyReportGenerator()
        self._health_monitor = HealthMonitor()
        self._last_triggered_week: Optional[str] = None

    def maybe_generate(
        self,
        bar_time: datetime,
        trails_this_week: list[DecisionTrail],
    ) -> Optional[str]:
        """
        Returns file path if report was generated, None otherwise.

        Trigger condition: Monday 00:00 UTC (bar_time.weekday() == 0
        and bar_time.hour == 0). The report covers the *previous* ISO week.
        """
        if bar_time.weekday() != 0 or bar_time.hour != 0:
            return None

        # ISO week of the *previous* week (the one just ended)
        # bar_time is Monday 00:00 — previous week ended just before this bar
        from datetime import timedelta

        prev_week_day = bar_time - timedelta(days=1)  # Sunday of the week that ended
        iso_year, iso_week, _ = prev_week_day.isocalendar()
        week_iso = f"{iso_year}-W{iso_week:02d}"

        # Avoid double-trigger for the same week boundary
        if week_iso == self._last_triggered_week:
            return None
        self._last_triggered_week = week_iso

        # Build health report from trail data using HealthMonitor
        # (lightweight: just use the empty monitor and pass trail-level data)
        health_report = self._health_monitor.generate()

        path = self._generator.generate(
            trails=trails_this_week,
            health_report=health_report,
            week_iso=week_iso,
            output_dir=self._output_dir,
        )
        return path
