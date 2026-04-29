"""
WeeklyGenerator — v2.0 §26.2 weekly report generator.

Generates a Markdown report covering the 7-day window ending at each
Sunday UTC 00:00.  Output path: sel_v2/reports/weekly/YYYY-WW.md

All data arrives via a plain dict (ReportData).  The generator itself has
no database dependency — callers (scheduler.py, tests) provide the data.

The auto-generated section covers §26.2 fields 1-9.
Three placeholder sections for manual annotation are always appended:
  - Wiki 視角
  - Claude 反馈
  - 調参決策記録
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from sel_v2.reports.sections import (
    build_state_distribution,
    build_cusum_stats,
    build_inverse_vocab_stats,
    build_strategy1_perf,
    build_strategy2_perf,
    build_coordination_events,
    build_anomalies,
    build_account_state,
    build_observation_tools,
)

_REPORTS_DIR = os.path.join(os.path.dirname(__file__), "weekly")


class WeeklyGenerator:
    """
    Generates the §26.2 weekly report from a data dict and writes to disk.

    Usage::

        gen = WeeklyGenerator()
        path = gen.generate(week_iso="2026-W18", data=report_data)
    """

    def __init__(self, output_dir: str = _REPORTS_DIR) -> None:
        self._output_dir = output_dir

    # ── Public API ────────────────────────────────────────────────────────

    def generate(
        self,
        week_iso: str,
        data: dict[str, Any],
        write_to_disk: bool = True,
    ) -> str:
        """
        Build the full weekly report Markdown and optionally save it.

        Parameters
        ----------
        week_iso:      ISO week string, e.g. "2026-W18"
        data:          Nested dict with keys matching each section builder.
                       Missing keys are silently treated as empty / N/A.
        write_to_disk: If True, write to output_dir/YYYY-WW.md.

        Returns
        -------
        str: The complete Markdown content.
        """
        now_utc = datetime.now(timezone.utc)
        period_start = data.get("period_start", "N/A")
        period_end = data.get("period_end", "N/A")

        md = self._header(week_iso, period_start, period_end, now_utc)
        md += self._body(data)
        md += self._manual_placeholders()

        if write_to_disk:
            path = self._write(week_iso, md)
            return path
        return md

    def generate_markdown(self, week_iso: str, data: dict[str, Any]) -> str:
        """Return the Markdown content without writing to disk."""
        return self.generate(week_iso, data, write_to_disk=False)

    # ── Private helpers ───────────────────────────────────────────────────

    def _header(
        self,
        week_iso: str,
        period_start: str,
        period_end: str,
        now_utc: datetime,
    ) -> str:
        gen_ts = now_utc.strftime("%Y-%m-%d %H:%M UTC")
        return (
            f"# Selene 週報 {week_iso}\n\n"
            f"## 週期情報\n\n"
            f"- 時間範囲: {period_start} 00:00 UTC ~ {period_end} 00:00 UTC\n"
            f"- 報告生成時間: {gen_ts}\n\n"
        )

    def _body(self, data: dict[str, Any]) -> str:
        sections = [
            build_state_distribution(data.get("state_distribution", {})),
            build_cusum_stats(data.get("cusum_stats", {})),
            build_inverse_vocab_stats(data.get("inverse_vocab_stats", {})),
            build_strategy1_perf(data.get("strategy1_perf", {})),
            build_strategy2_perf(data.get("strategy2_perf", {})),
            build_coordination_events(data.get("coordination_events", {})),
            build_anomalies(data.get("anomalies", {})),
            build_account_state(data.get("account_state", {})),
            build_observation_tools(data.get("observation_tools", {})),
        ]
        return "\n---\n\n".join(s.strip() for s in sections) + "\n\n---\n\n"

    def _manual_placeholders(self) -> str:
        return (
            "## Wiki 視角（Wiki 手動追加）\n\n"
            "[Wiki 在此 append — 異常情況 / 直覚判断 / 関心問題]\n\n"
            "---\n\n"
            "## Claude 反馈（次回対話生成, Wiki 貼り付け可）\n\n"
            "[Claude 反馈]\n\n"
            "---\n\n"
            "## 調参決策記録\n\n"
            "[本週是否調参, 調了什么 — 必须与 decision_trail 同步]\n"
        )

    def _write(self, week_iso: str, content: str) -> str:
        os.makedirs(self._output_dir, exist_ok=True)
        fname = f"{week_iso}.md"
        path = os.path.join(self._output_dir, fname)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path
