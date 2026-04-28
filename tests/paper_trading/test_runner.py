"""Tests for PaperTradingRunner._get_none_reasons_in_window.

Only tests the none_reason collection helper — full runner integration requires
a live StateOutputService (DB + Redis) and is not covered here.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Optional

_T = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
_SYMBOL = "BTCUSDT"


def _make_runner():
    """Create a PaperTradingRunner stub with only _recent_none_reasons set.

    Uses object.__new__ to bypass __init__ (which imports StateOutputService and
    requires DB/Redis) — we only need to test _get_none_reasons_in_window.
    """
    from paper_trading.runner import PaperTradingRunner
    runner = object.__new__(PaperTradingRunner)
    runner._recent_none_reasons = deque(maxlen=720)
    return runner


class TestRunnerNoneReasonCollection:
    """_get_none_reasons_in_window counts bar none_reasons in a half-open (start, end] window."""

    def test_empty_deque_returns_zero_counts(self):
        runner = _make_runner()
        result = runner._get_none_reasons_in_window(_T, _T + timedelta(hours=3))
        assert result == {"missing_data": 0, "no_match": 0, "cold_start": 0}

    def test_counts_missing_data_and_no_match_separately(self):
        runner = _make_runner()
        t1 = _T + timedelta(hours=1)
        t2 = _T + timedelta(hours=2)
        t3 = _T + timedelta(hours=3)
        runner._recent_none_reasons.extend([
            (t1, "missing_data"),
            (t2, "no_match"),
            (t3, "missing_data"),
        ])
        result = runner._get_none_reasons_in_window(_T, t3)
        assert result["missing_data"] == 2
        assert result["no_match"] == 1
        assert result["cold_start"] == 0

    def test_excludes_start_time_bar_is_exclusive(self):
        """The window is (start, end] — bar exactly at start is excluded."""
        runner = _make_runner()
        runner._recent_none_reasons.append((_T, "missing_data"))
        result = runner._get_none_reasons_in_window(_T, _T + timedelta(hours=1))
        assert result["missing_data"] == 0  # excluded (bar == start, not > start)

    def test_includes_end_time_bar_is_inclusive(self):
        """Bar exactly at end is included."""
        runner = _make_runner()
        end = _T + timedelta(hours=2)
        runner._recent_none_reasons.append((end, "no_match"))
        result = runner._get_none_reasons_in_window(_T, end)
        assert result["no_match"] == 1

    def test_skips_bars_with_none_reason_none(self):
        """Active state bars have none_reason=None and must not be counted."""
        runner = _make_runner()
        t1 = _T + timedelta(hours=1)
        runner._recent_none_reasons.append((t1, None))  # active bar
        result = runner._get_none_reasons_in_window(_T, t1)
        assert sum(result.values()) == 0

    def test_mixed_18_missing_6_no_match(self):
        """Scenario from task spec: 18 missing_data + 6 no_match in 24H lag."""
        runner = _make_runner()
        for i in range(1, 19):
            runner._recent_none_reasons.append((_T + timedelta(hours=i), "missing_data"))
        for i in range(19, 25):
            runner._recent_none_reasons.append((_T + timedelta(hours=i), "no_match"))
        end = _T + timedelta(hours=24)
        result = runner._get_none_reasons_in_window(_T, end)
        assert result["missing_data"] == 18
        assert result["no_match"] == 6
