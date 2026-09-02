"""Tests for sel_v2.tools.golive_report (GL1 T0.6)."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import sel_v2.tools.golive_report as report_mod
from sel_v2.tools.golive_report import CheckResult, GateReport


def test_check_result_color_follows_ok():
    assert CheckResult("x", True, "fine").color == "GREEN"
    assert CheckResult("x", False, "broken").color == "RED"


def test_gate_report_overall_red_if_any_check_fails():
    checks = [
        CheckResult("a", True, "ok"),
        CheckResult("b", False, "gap: field X missing"),
    ]
    report = GateReport(
        gate="G0",
        generated_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        checks=checks,
        overall="RED",
    )
    rendered = report.render()
    assert "[RED] b: gap: field X missing" in rendered
    assert "[GREEN] a: ok" in rendered
    assert "OVERALL: RED" in rendered


def _mock_pool(**fetch_returns):
    """fetch_returns maps a marker substring in the SQL to a canned return value for
    conn.fetch/fetchval, matched in call order via side_effect lists is overkill here —
    tests instead patch the individual helper coroutines directly (simpler, avoids
    over-fitting to SQL text)."""
    pool = MagicMock()
    return pool


def test_none_prone_fill_rates_reads_decision_trail(monkeypatch):
    async def run():
        conn = AsyncMock()
        conn.fetch = AsyncMock(
            return_value=[
                {
                    "decision_trail": json.dumps(
                        {
                            "entropy_variance_rising": True,
                            "oi_change_rate": None,
                            "funding_persistent": True,
                        }
                    )
                },
                {
                    "decision_trail": json.dumps(
                        {
                            "entropy_variance_rising": None,
                            "oi_change_rate": 0.1,
                            "funding_persistent": True,
                        }
                    )
                },
            ]
        )
        pool = MagicMock()
        pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
        pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
        import datetime

        rates = await report_mod._none_prone_fill_rates(pool, datetime.datetime.now(datetime.timezone.utc))
        assert rates["entropy_variance_rising"] == 0.5
        assert rates["oi_change_rate"] == 0.5
        assert rates["funding_persistent"] == 1.0

    asyncio.run(run())


def test_none_prone_fill_rates_empty_is_all_zero():
    async def run():
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[])
        pool = MagicMock()
        pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
        pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
        import datetime

        rates = await report_mod._none_prone_fill_rates(pool, datetime.datetime.now(datetime.timezone.utc))
        assert all(v == 0.0 for v in rates.values())

    asyncio.run(run())


def test_build_g0_report_red_when_no_epoch_and_empty_data(monkeypatch):
    """Mirrors the live smoke test: no epoch, no decision_trail data yet -> RED with
    itemized gaps (GL1 T0.6 acceptance)."""

    async def fake_status(pool):
        from sel_v2.tools.epoch import EpochStatus

        return EpochStatus(
            epoch_id=None,
            started_at=None,
            reason=None,
            fingerprint_at_start=None,
            current_fingerprint="fp",
            git_hash_at_start=None,
            current_git_hash="unknown",
            status="NO_EPOCH",
        )

    async def fake_fill_rates(pool, since):
        return {
            "entropy_variance_rising": 0.0,
            "oi_change_rate": 0.0,
            "funding_persistent": 0.0,
        }

    async def fake_dist(pool, since):
        return {}

    async def fake_liveness(pool, since):
        return (0, 0)

    async def fake_staleness(pool, since):
        return ([], [])

    monkeypatch.setattr(report_mod.epoch_mod, "status", fake_status)
    monkeypatch.setattr(report_mod, "_none_prone_fill_rates", fake_fill_rates)
    monkeypatch.setattr(report_mod, "_state_distribution", fake_dist)
    monkeypatch.setattr(report_mod, "_decision_trail_liveness", fake_liveness)
    monkeypatch.setattr(report_mod, "_staleness_summary", fake_staleness)

    async def run():
        report = await report_mod.build_g0_report(pool=MagicMock())
        assert report.overall == "RED"
        names = {c.name: c for c in report.checks}
        assert names["epoch"].ok is False
        assert names["fill_rate:entropy_variance_rising"].ok is False
        assert names["P2-1_decision_trail"].ok is False
        return report

    report = asyncio.run(run())
    assert (
        "no v2_strategy_decision rows in window"
        in [c.detail for c in report.checks if c.name == "P2-1_decision_trail"][0]
    )


def test_build_g0_report_green_when_everything_passes(monkeypatch):
    async def fake_status(pool):
        import datetime

        from sel_v2.tools.epoch import EpochStatus

        return EpochStatus(
            epoch_id="e1",
            started_at=datetime.datetime.now(datetime.timezone.utc),
            reason="G0 confirmed",
            fingerprint_at_start="fp",
            current_fingerprint="fp",
            git_hash_at_start="abc",
            current_git_hash="abc",
            status="CLEAN",
        )

    async def fake_fill_rates(pool, since):
        return {
            "entropy_variance_rising": 1.0,
            "oi_change_rate": 1.0,
            "funding_persistent": 1.0,
        }

    async def fake_dist(pool, since):
        return {"Coiling": 10, "Surging": 5}

    async def fake_liveness(pool, since):
        return (100, 100)

    async def fake_staleness(pool, since):
        return ([], [])

    monkeypatch.setattr(report_mod.epoch_mod, "status", fake_status)
    monkeypatch.setattr(report_mod, "_none_prone_fill_rates", fake_fill_rates)
    monkeypatch.setattr(report_mod, "_state_distribution", fake_dist)
    monkeypatch.setattr(report_mod, "_decision_trail_liveness", fake_liveness)
    monkeypatch.setattr(report_mod, "_staleness_summary", fake_staleness)

    async def run():
        return await report_mod.build_g0_report(pool=MagicMock())

    report = asyncio.run(run())
    assert report.overall == "GREEN"


def test_build_g0_report_red_when_source_currently_stale(monkeypatch):
    async def fake_status(pool):
        import datetime

        from sel_v2.tools.epoch import EpochStatus

        return EpochStatus(
            epoch_id="e1",
            started_at=datetime.datetime.now(datetime.timezone.utc),
            reason="G0 confirmed",
            fingerprint_at_start="fp",
            current_fingerprint="fp",
            git_hash_at_start="abc",
            current_git_hash="abc",
            status="CLEAN",
        )

    async def fake_fill_rates(pool, since):
        return {
            "entropy_variance_rising": 1.0,
            "oi_change_rate": 1.0,
            "funding_persistent": 1.0,
        }

    async def fake_dist(pool, since):
        return {}

    async def fake_liveness(pool, since):
        return (100, 100)

    async def fake_staleness(pool, since):
        return (["ticks"], [{"source": "ticks", "stale": True}])

    monkeypatch.setattr(report_mod.epoch_mod, "status", fake_status)
    monkeypatch.setattr(report_mod, "_none_prone_fill_rates", fake_fill_rates)
    monkeypatch.setattr(report_mod, "_state_distribution", fake_dist)
    monkeypatch.setattr(report_mod, "_decision_trail_liveness", fake_liveness)
    monkeypatch.setattr(report_mod, "_staleness_summary", fake_staleness)

    async def run():
        return await report_mod.build_g0_report(pool=MagicMock())

    report = asyncio.run(run())
    assert report.overall == "RED"
    stale_check = next(c for c in report.checks if c.name == "staleness_current")
    assert stale_check.ok is False
