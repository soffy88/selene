"""Rich decision-trail read API (audit P1-7).

The per-bar `sel_decision_trail` (full WHY of each decision: features, state+reason, proposed
vs final action, matched rule, risk veto, fill) was persisted but had no read surface, so the
Helios "moat" was invisible to operators. The new /sel/decision-trail/full endpoint exposes
it, and degrades to [] when the table doesn't exist yet.
"""
import asyncio
from datetime import datetime, timezone

import sel_v2.paper_interface.api as api


class _Conn:
    def __init__(self, rows=None, raise_on_fetch=False):
        self._rows = rows or []
        self._raise = raise_on_fetch
        self.sql = None

    async def fetch(self, sql, *args):
        self.sql = sql
        if self._raise:
            raise RuntimeError('relation "sel_decision_trail" does not exist')
        return self._rows


class _Pool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        conn = self._conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *a):
                return False
        return _Ctx()


def _patch_pool(monkeypatch, conn):
    async def fake_pool():
        return _Pool(conn)
    monkeypatch.setattr(api, "get_pool", fake_pool)


def test_full_trail_returns_rich_rows(monkeypatch):
    row = {
        "time": datetime(2024, 1, 1, tzinfo=timezone.utc), "symbol": "BTC-USDT",
        "current_state": "Coiling", "state_reason": "σ compressing",
        "proposed_action": "OPEN_LONG", "final_action": "HOLD",
        "rule_id": "surging_up_from_coiling", "risk_triggered": True,
        "risk_details": "drawdown_halt", "config_hash": "abc123",
    }
    conn = _Conn(rows=[row])
    _patch_pool(monkeypatch, conn)
    out = asyncio.run(api.decision_trail_full(symbol="BTC-USDT", limit=10))
    assert len(out) == 1
    d = out[0]
    # the WHY fields are surfaced, and datetime is serialised
    assert d["proposed_action"] == "OPEN_LONG" and d["final_action"] == "HOLD"
    assert d["rule_id"] == "surging_up_from_coiling" and d["risk_triggered"] is True
    assert isinstance(d["time"], str)
    assert "sel_decision_trail" in conn.sql


def test_full_trail_degrades_to_empty_when_table_absent(monkeypatch):
    conn = _Conn(raise_on_fetch=True)
    _patch_pool(monkeypatch, conn)
    out = asyncio.run(api.decision_trail_full(symbol="BTC-USDT"))
    assert out == []   # graceful, not a 500


# ── strategy summary (S1/S2 frontend panel) ─────────────────────────────────

class _SummaryConn:
    """strategy_summary issues two fetch()es (aggregates, then latest decisions), one
    fetchrow() (state) and one fetchval() (bars). Return them in order."""
    def __init__(self, agg_rows, decision_rows, state_row, bars):
        self._fetches = [agg_rows, decision_rows]
        self._state, self._bars = state_row, bars

    async def fetch(self, *a):
        return self._fetches.pop(0) if self._fetches else []

    async def fetchrow(self, *a):
        return self._state

    async def fetchval(self, *a):
        return self._bars


def test_strategy_summary_partitions_and_zero_fills(monkeypatch):
    agg = [
        {"strategy": "strategy_1", "open_trades": 1, "closed_trades": 4, "wins": 3,
         "realized_pnl": 125.50},
        # strategy_2 absent → must zero-fill, not error
    ]
    decisions = [
        {"strategy": "strategy_1", "action": "ABORT", "reason": "Step 1: state filter",
         "step_reached": 1, "state_4h": "Surging", "direction": None,
         "timestamp": datetime(2026, 6, 30, tzinfo=timezone.utc), "updated_at": None},
    ]
    state_row = {"state": "Surging", "timestamp": datetime(2026, 6, 30, tzinfo=timezone.utc)}
    _patch_pool(monkeypatch, _SummaryConn(agg, decisions, state_row, 4487))
    out = asyncio.run(api.strategy_summary(symbol="BTC-USDT"))
    s1 = out["strategy_1"]
    assert s1["open_trades"] == 1 and s1["closed_trades"] == 4 and s1["win_rate"] == 0.75
    # the "why no entry" decision is attached
    assert s1["last_decision"]["action"] == "ABORT" and s1["last_decision"]["step_reached"] == 1
    # absent strategy is present, zeroed, with no decision
    assert out["strategy_2"]["closed_trades"] == 0 and out["strategy_2"]["last_decision"] is None
    assert out["current_state"] == "Surging" and out["total_bars"] == 4487


def test_strategy_summary_empty_is_valid(monkeypatch):
    _patch_pool(monkeypatch, _SummaryConn([], [], None, 0))
    out = asyncio.run(api.strategy_summary(symbol="BTC-USDT"))
    assert out["strategy_1"]["closed_trades"] == 0
    assert out["strategy_1"]["last_decision"] is None
    assert out["current_state"] is None


def test_state_history_derives_completeness_and_coldstart(monkeypatch):
    rows = [{
        "timestamp": datetime(2026, 6, 30, 4, tzinfo=timezone.utc),
        "state": "Surging", "transition_from": "Cascade", "transition_via": "Exhaustion",
        "duration_4h": 3,
        "state_features": {"a": 1, "b": None, "c": 2, "d": None, "cold_start": False},
        "s1_action": "OBSERVE", "s1_step": 1, "s1_reason": "Step 1: state filter",
    }]
    _patch_pool(monkeypatch, _Conn(rows=rows))
    out = asyncio.run(api.state_history(symbol="BTC-USDT"))
    r = out[0]
    assert r["state"] == "Surging" and r["transition_from"] == "Cascade"
    assert r["transition_via"] == "Exhaustion" and r["duration_4h"] == 3
    # 3 of 5 feature keys non-null
    assert r["feature_completeness"] == 0.6
    assert r["cold_start"] is False
    # per-bar S1 decision joined in (#2)
    assert r["s1_action"] == "OBSERVE" and r["s1_step"] == 1
    # the fake direction/confidence columns are gone
    assert "direction" not in r and "confidence" not in r


def test_state_history_handles_jsonb_string(monkeypatch):
    import json as _json
    rows = [{
        "timestamp": datetime(2026, 6, 30, tzinfo=timezone.utc), "state": "Cascade",
        "transition_from": None, "transition_via": None, "duration_4h": 1,
        "state_features": _json.dumps({"x": 1, "cold_start": True}),   # asyncpg may hand back a str
        "s1_action": None, "s1_step": None, "s1_reason": None,
    }]
    _patch_pool(monkeypatch, _Conn(rows=rows))
    out = asyncio.run(api.state_history(symbol="BTC-USDT"))
    assert out[0]["cold_start"] is True and out[0]["feature_completeness"] == 1.0


def test_sel_chart_returns_ascending_ohlc_with_state(monkeypatch):
    import decimal
    # endpoint queries DESC; it must return ascending unix-second OHLC + state
    t1 = datetime(2026, 6, 30, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 6, 30, 4, tzinfo=timezone.utc)
    rows = [  # DESC order as the SQL returns
        {"time": t2, "open": decimal.Decimal("2"), "high": decimal.Decimal("3"),
         "low": decimal.Decimal("1"), "close": decimal.Decimal("2.5"), "state": "Surging"},
        {"time": t1, "open": decimal.Decimal("1"), "high": decimal.Decimal("2"),
         "low": decimal.Decimal("0.5"), "close": decimal.Decimal("1.5"), "state": "Drifting_Calm"},
    ]
    _patch_pool(monkeypatch, _Conn(rows=rows))
    out = asyncio.run(api.sel_chart(symbol="BTC-USDT", bars=300))
    assert [d["state"] for d in out] == ["Drifting_Calm", "Surging"]   # ascending
    assert out[0]["time"] == int(t1.timestamp()) and isinstance(out[0]["time"], int)
    assert out[1]["close"] == 2.5 and out[1]["high"] == 3.0


def test_observations_endpoint_adds_names(monkeypatch):
    rows = [
        {"tool_id": "B1", "source": "hmm", "signal": False, "value": 0.07, "threshold": 0.6,
         "label": "LOW_VOL", "confidence": 0.93, "timestamp": datetime(2026, 6, 30, tzinfo=timezone.utc),
         "updated_at": datetime(2026, 6, 30, tzinfo=timezone.utc)},
        {"tool_id": "H3", "source": "hawkes", "signal": True, "value": 300.0, "threshold": 212.0,
         "label": "WARNING", "confidence": 0.8, "timestamp": datetime(2026, 6, 30, tzinfo=timezone.utc),
         "updated_at": datetime(2026, 6, 30, tzinfo=timezone.utc)},
    ]
    _patch_pool(monkeypatch, _Conn(rows=rows))
    out = asyncio.run(api.sel_observations())
    assert len(out) == 2
    assert out[0]["tool_id"] == "B1" and out[0]["name"]        # human name attached
    assert out[1]["signal"] is True and out[1]["label"] == "WARNING"


def test_observations_graceful_when_table_absent(monkeypatch):
    _patch_pool(monkeypatch, _Conn(raise_on_fetch=True))
    assert asyncio.run(api.sel_observations()) == []


def test_run_recent_observations_returns_seven_tools():
    import numpy as np
    import pandas as pd
    from sel_v2.observation_tools.runner import run_recent_observations
    rng = np.random.default_rng(3)
    n = 600
    close = 30000 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    df = pd.DataFrame({"time": [pd.Timestamp("2024-01-01") + pd.Timedelta(hours=4 * i) for i in range(n)],
                       "close": close, "volume": 1000.0})
    res = run_recent_observations(df, window=540)
    assert len(res) == 7
    assert {r.tool_id for r in res} == {"B1", "B2", "TDA2", "I1", "T2", "W2", "H3"}
    assert all(hasattr(r, "signal") and hasattr(r, "value") for r in res)


def test_counterfactual_summarises_trades(monkeypatch):
    e1 = datetime(2024, 7, 15, tzinfo=timezone.utc); x1 = datetime(2024, 7, 22, tzinfo=timezone.utc)
    e2 = datetime(2024, 9, 6, tzinfo=timezone.utc); x2 = datetime(2024, 9, 9, tzinfo=timezone.utc)
    rows = [
        {"strategy": "strategy_1", "direction": "SHORT", "entry_time": e1, "entry_price": 67000.0,
         "exit_time": x1, "exit_price": 53000.0, "pnl_usdt": 7665.0, "exit_reason": "Time stop"},
        {"strategy": "strategy_1", "direction": "SHORT", "entry_time": e2, "entry_price": 54000.0,
         "exit_time": x2, "exit_price": 57000.0, "pnl_usdt": -2214.0, "exit_reason": "Drawdown stop"},
    ]
    _patch_pool(monkeypatch, _Conn(rows=rows))
    out = asyncio.run(api.sel_counterfactual())
    assert out["summary"]["n_trades"] == 2
    assert out["summary"]["win_rate"] == 0.5
    assert out["summary"]["net_pnl_usdt"] == 7665.0 - 2214.0
    # entry/exit times are unix seconds for the chart
    assert out["trades"][0]["entry_time"] == int(e1.timestamp())
    assert out["trades"][0]["direction"] == "SHORT"


def test_counterfactual_graceful_when_table_absent(monkeypatch):
    _patch_pool(monkeypatch, _Conn(raise_on_fetch=True))
    out = asyncio.run(api.sel_counterfactual())
    assert out == {"trades": [], "summary": None}


def test_decision_view_flattens_entry_decision():
    from sel_v2.paper.strategy_engine import PaperStrategyEngine

    class _D:
        action = "OBSERVE"; reason = "Step 1b: Hawkes λ* below threshold"
        step_reached = 2; direction = None; cusum_direction = "LONG"
    view = PaperStrategyEngine._decision_view(
        (datetime(2026, 6, 30, tzinfo=timezone.utc), "Surging", _D()))
    assert view["action"] == "OBSERVE" and view["step_reached"] == 2
    assert view["state_4h"] == "Surging" and view["direction"] == "LONG"
    assert PaperStrategyEngine._decision_view(None) is None


def test_decision_trail_emits_bounded_rows():
    from sel_v2.paper.strategy_engine import PaperStrategyEngine

    class _D:
        def __init__(self, a): self.action = a; self.reason = "r"; self.step_reached = 1
        direction = None; cusum_direction = None
    eng = PaperStrategyEngine.__new__(PaperStrategyEngine)   # bypass __post_init__
    ts0 = datetime(2026, 6, 1, tzinfo=timezone.utc)
    eng._s1_trail = [(ts0, "Surging", _D("OBSERVE")) for _ in range(500)]
    eng._s2_trail = [(ts0, "Surging", _D("ABORT")) for _ in range(10)]
    rows = eng.decision_trail(last_n=300)
    # bounded to 300 per strategy: 300 (S1) + 10 (S2)
    assert len(rows) == 310
    ts, strat, action, reason, step, state, direction = rows[0]
    assert strat == "strategy_1" and action == "OBSERVE" and step == 1


def test_write_decision_trail_bulk_upserts(monkeypatch):
    import asyncio as _aio
    from unittest.mock import AsyncMock
    from sel_v2.strategies.db_writer import DBWriter
    w = DBWriter()
    w._conn = AsyncMock()
    rows = [(datetime(2026, 6, 1, tzinfo=timezone.utc), "strategy_1", "OBSERVE", "r", 1, "Surging", None)]
    n = _aio.run(w.write_decision_trail_bulk(rows))
    assert n == 1
    sql = w._conn.executemany.call_args[0][0]
    assert "ON CONFLICT (timestamp, strategy) DO UPDATE" in sql and "DO NOTHING" not in sql
