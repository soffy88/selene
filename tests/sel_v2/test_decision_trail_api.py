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
    """Returns per-strategy aggregate rows for fetch(), a state row for fetchrow(),
    and a bar count for fetchval() — matching strategy_summary's three queries."""
    def __init__(self, rows, state_row, bars):
        self._rows, self._state, self._bars = rows, state_row, bars

    async def fetch(self, *a):
        return self._rows

    async def fetchrow(self, *a):
        return self._state

    async def fetchval(self, *a):
        return self._bars


def test_strategy_summary_partitions_and_zero_fills(monkeypatch):
    rows = [
        {"strategy": "strategy_1", "open_trades": 1, "closed_trades": 4, "wins": 3,
         "realized_pnl": 125.50},
        # strategy_2 absent → must zero-fill, not error
    ]
    state_row = {"state": "Surging", "timestamp": datetime(2026, 6, 30, tzinfo=timezone.utc)}
    _patch_pool(monkeypatch, _SummaryConn(rows, state_row, 4487))
    out = asyncio.run(api.strategy_summary(symbol="BTC-USDT"))
    assert out["strategy_1"] == {"strategy": "strategy_1", "open_trades": 1,
                                 "closed_trades": 4, "realized_pnl": 125.5, "win_rate": 0.75}
    # absent strategy is present and zeroed (win_rate None when no closed trades)
    assert out["strategy_2"]["closed_trades"] == 0 and out["strategy_2"]["win_rate"] is None
    assert out["current_state"] == "Surging"
    assert out["total_bars"] == 4487


def test_strategy_summary_empty_is_valid(monkeypatch):
    _patch_pool(monkeypatch, _SummaryConn([], None, 0))
    out = asyncio.run(api.strategy_summary(symbol="BTC-USDT"))
    assert out["strategy_1"]["closed_trades"] == 0
    assert out["current_state"] is None
