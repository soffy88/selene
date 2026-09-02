"""
sel_v2 weekly report generation  (v2.0 §26).

WeeklyGenerator produces a Markdown report each Sunday UTC 00:00.
Sections follow v2.0 §26.2 exactly.
"""

from sel_v2.reports.weekly_generator import WeeklyGenerator

__all__ = ["WeeklyGenerator"]
