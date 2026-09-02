"""Tests for PaperTradingRunner._get_none_reasons_in_window and _emit_rule2_alert_if_needed.

Only tests the none_reason collection helper and alert dedup logic — full runner
integration requires a live StateOutputService (DB + Redis) and is not covered here.
"""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

_T = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
_SYMBOL = "BTCUSDT"


def _make_runner(dedup_hours: int = 6):
    """Create a PaperTradingRunner stub with minimal internal state.

    Uses object.__new__ to bypass __init__ (which imports StateOutputService and
    requires DB/Redis) — we only need to test window helpers and alert dedup.
    """
    from paper_trading.runner import PaperTradingRunner

    runner = object.__new__(PaperTradingRunner)
    runner._recent_none_reasons = deque(maxlen=720)
    runner._last_alert_time = None
    runner.symbol = _SYMBOL
    # Minimal config mock: only risk.missing_data_alert_dedup_hours is needed
    risk_cfg = MagicMock()
    risk_cfg.missing_data_alert_dedup_hours = dedup_hours
    cfg = MagicMock()
    cfg.risk = risk_cfg
    runner.config = cfg
    return runner


def _make_trail_stub(
    alert_required: bool = True,
    risk_details: str = "State signal lag 3.0H > 2H [subtype=missing_data]",
    lag_reasons: Optional[dict] = None,
    position_side: Optional[str] = None,
    position_size_usdt: float = 0.0,
    unrealized_pnl_usdt: float = 0.0,
) -> MagicMock:
    """Minimal DecisionTrail-like stub for alert tests."""
    trail = MagicMock()
    trail.alert_required = alert_required
    trail.risk_details = risk_details
    trail.state_none_reason_in_lag = lag_reasons or {"missing_data": 18, "no_match": 6, "cold_start": 0}
    trail.position_side = position_side
    trail.position_size_usdt = position_size_usdt
    trail.unrealized_pnl_usdt = unrealized_pnl_usdt
    return trail


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
        runner._recent_none_reasons.extend(
            [
                (t1, "missing_data"),
                (t2, "no_match"),
                (t3, "missing_data"),
            ]
        )
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


# ---------------------------------------------------------------------------
# Test: Rule 2 alert emission with deduplication
# ---------------------------------------------------------------------------


class TestRule2AlertDedup:
    """_emit_rule2_alert_if_needed rate-limits alerts to one per dedup_hours window."""

    def test_alert_fires_on_first_missing_data_entry(self):
        runner = _make_runner(dedup_hours=6)
        redis = AsyncMock()
        trail = _make_trail_stub()

        asyncio.run(runner._emit_rule2_alert_if_needed(redis, trail, _T))

        redis.xadd.assert_called_once()
        assert runner._last_alert_time == _T

    def test_alert_dedup_suppresses_within_6h(self):
        """Second alert within dedup window must not call redis.xadd again."""
        runner = _make_runner(dedup_hours=6)
        redis = AsyncMock()
        trail = _make_trail_stub()

        asyncio.run(runner._emit_rule2_alert_if_needed(redis, trail, _T))
        # 3H later — still within 6H dedup window
        asyncio.run(runner._emit_rule2_alert_if_needed(redis, trail, _T + timedelta(hours=3)))

        assert redis.xadd.call_count == 1  # only the first call went through

    def test_alert_fires_again_after_dedup_window(self):
        """After 6H dedup window, the next alert must fire."""
        runner = _make_runner(dedup_hours=6)
        redis = AsyncMock()
        trail = _make_trail_stub()

        asyncio.run(runner._emit_rule2_alert_if_needed(redis, trail, _T))
        # 7H later — beyond 6H dedup window
        asyncio.run(runner._emit_rule2_alert_if_needed(redis, trail, _T + timedelta(hours=7)))

        assert redis.xadd.call_count == 2

    def test_alert_content_includes_risk_alert_type(self):
        """Published message must have type=risk_alert for NotificationHub routing."""
        runner = _make_runner(dedup_hours=6)
        redis = AsyncMock()
        trail = _make_trail_stub()

        asyncio.run(runner._emit_rule2_alert_if_needed(redis, trail, _T))

        call_args = redis.xadd.call_args
        stream_name = call_args[0][0]
        fields = call_args[0][1]
        assert stream_name == "system.alerts"
        assert fields.get("type") == "risk_alert"
        assert fields.get("rule_2_subtype") == "missing_data"
