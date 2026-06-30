import os
import json
import asyncpg
from fastapi import APIRouter, Query, HTTPException
from typing import Dict, List, Optional
from pydantic import BaseModel
from datetime import datetime

router = APIRouter()
pool = None

async def get_pool():
    global pool
    if pool is None:
        db_url = os.environ.get("DB_URL")
        if not db_url:
            raise RuntimeError("DB_URL must be set (no hardcoded credential fallback)")
        pool = await asyncpg.create_pool(db_url)
    return pool

class StateHealth(BaseModel):
    symbol: str
    total_bars: int  # Redundant for frontend compatibility
    total_bars_4h: int
    cold_start_bars: int
    active_bars: int
    satiation_pct: float
    is_ready: bool
    latest_ts: Optional[datetime]
    state_counts: Dict[str, int] = {}
    health_warning_count: int = 0

def normalize_symbol(symbol: str) -> str:
    if symbol == "BTCUSDT": return "BTC-USDT"
    if symbol == "ETHUSDT": return "ETH-USDT"
    return symbol

def record_to_dict(record):
    if not record: return None
    d = dict(record)
    for k, v in d.items():
        if hasattr(v, 'quantize'):
            d[k] = float(v)
        elif isinstance(v, datetime):
            d[k] = v.isoformat()
    return d

@router.get("/sel/state/health", response_model=StateHealth)
async def state_health(symbol: str = Query("BTC-USDT")):
    symbol = normalize_symbol(symbol)
    p = await get_pool()
    async with p.acquire() as conn:
        count = await conn.fetchval("SELECT count(*) FROM v2_bars_4h WHERE symbol=$1", symbol)
        latest = await conn.fetchval("SELECT max(time) FROM v2_bars_4h WHERE symbol=$1", symbol)
        sc_rows = await conn.fetch(
            "SELECT state, count(*)::int AS cnt FROM v2_state_history "
            "WHERE timestamp > NOW() - INTERVAL '24 hours' GROUP BY state"
        )
        warn_count = await conn.fetchval(
            "SELECT count(*) FROM v2_state_history "
            "WHERE timestamp > NOW() - INTERVAL '30 days' AND state IS NULL"
        )

        count = count or 0
        satiation = min(count / 180 * 100, 100.0)
        is_ready = count >= 180
        state_counts = {r["state"]: r["cnt"] for r in sc_rows if r["state"]}

        return StateHealth(
            symbol=symbol,
            total_bars=count,
            total_bars_4h=count,
            cold_start_bars=count,
            active_bars=count,
            satiation_pct=round(satiation, 2),
            is_ready=is_ready,
            latest_ts=latest,
            state_counts=state_counts,
            health_warning_count=int(warn_count or 0),
        )

@router.get("/sel/state/history")
async def state_history(symbol: str = Query("BTC-USDT"), hours: int = 24, limit: int = 100):
    """Recent regime states with the fields that actually exist in v2_state_history:
    the state, where it transitioned FROM, the transition trigger (Release/Stress/Exhaustion),
    how long it has held (duration_4h), feature completeness (computed from state_features),
    and cold-start. The old 'direction'/'confidence' columns had no backing data — the state
    machine is deterministic and never populated sub_state — so they are dropped."""
    symbol = normalize_symbol(symbol)
    p = await get_pool()
    async with p.acquire() as conn:
        rows = await conn.fetch(
            "SELECT timestamp, state, transition_from, transition_via, duration_4h, state_features "
            "FROM v2_state_history ORDER BY timestamp DESC LIMIT $1", limit
        )
    out = []
    for r in rows:
        f = r["state_features"]
        if isinstance(f, str):
            f = json.loads(f)
        f = f or {}
        total = len(f)
        non_null = sum(1 for v in f.values() if v is not None)
        out.append({
            "time": r["timestamp"].isoformat(),
            "state": r["state"],
            "transition_from": r["transition_from"],
            "transition_via": r["transition_via"],
            "duration_4h": r["duration_4h"],
            "feature_completeness": round(non_null / total, 3) if total else None,
            "cold_start": bool(f.get("cold_start")) if "cold_start" in f else None,
        })
    return out

@router.get("/sel/chart")
async def sel_chart(symbol: str = Query("BTC-USDT"), bars: int = 300):
    """OHLC bars + the regime state per bar for the SEL price chart. Joins v2_bars_4h with
    v2_state_history on the bar open time; returns the most recent `bars` 4H bars ascending.
    `time` is unix seconds (the time format lightweight-charts expects)."""
    sym = normalize_symbol(symbol)
    p = await get_pool()
    async with p.acquire() as conn:
        rows = await conn.fetch(
            "SELECT b.time, b.open, b.high, b.low, b.close, s.state "
            "FROM v2_bars_4h b "
            "LEFT JOIN v2_state_history s ON s.timestamp = b.time "
            "WHERE b.symbol = $1 "
            "ORDER BY b.time DESC LIMIT $2", sym, bars)
    return [
        {"time": int(r["time"].timestamp()),
         "open": float(r["open"]), "high": float(r["high"]),
         "low": float(r["low"]), "close": float(r["close"]),
         "state": r["state"]}
        for r in reversed(rows)
    ]


@router.get("/sel/cusum/recent")
async def cusum_recent(strategy: str = "all", limit: int = 20):
    p = await get_pool()
    async with p.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM v2_cusum_events ORDER BY timestamp DESC LIMIT $1", limit
        )
        return [record_to_dict(r) for r in rows]

@router.get("/sel/trades/open")
async def trades_open():
    p = await get_pool()
    async with p.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM v2_trades WHERE exit_time IS NULL ORDER BY entry_time DESC"
        )
        return [record_to_dict(r) for r in rows]

@router.get("/sel/trades/recent")
async def trades_recent(limit: int = 20):
    p = await get_pool()
    async with p.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM v2_trades WHERE exit_time IS NOT NULL ORDER BY exit_time DESC LIMIT $1", limit
        )
        return [record_to_dict(r) for r in rows]

@router.get("/sel/strategy/summary")
async def strategy_summary(symbol: str = Query("BTC-USDT")):
    """Per-strategy (S1 trend / S2 intraday) paper status for the UI: open/closed trade
    counts, realised PnL and win-rate, plus the current regime state and bar coverage.

    Built only from v2_trades + v2_state_history (the PG the API already uses; no Redis).
    Zeroes when a strategy hasn't traded yet — that's a valid observation (e.g. S1 sits out
    a sustained high-volatility regime because its entry states never form), not an error."""
    p = await get_pool()
    sym = normalize_symbol(symbol)
    async with p.acquire() as conn:
        rows = await conn.fetch(
            "SELECT strategy, "
            "  count(*) FILTER (WHERE exit_time IS NULL)     AS open_trades, "
            "  count(*) FILTER (WHERE exit_time IS NOT NULL) AS closed_trades, "
            "  count(*) FILTER (WHERE exit_time IS NOT NULL AND pnl_usdt > 0) AS wins, "
            "  round(coalesce(sum(pnl_usdt) FILTER (WHERE exit_time IS NOT NULL), 0)::numeric, 2) AS realized_pnl "
            "FROM v2_trades GROUP BY strategy")
        latest = await conn.fetchrow(
            "SELECT state, timestamp FROM v2_state_history ORDER BY timestamp DESC LIMIT 1")
        bars = await conn.fetchval("SELECT count(*) FROM v2_bars_4h WHERE symbol = $1", sym)
        # Latest per-strategy decision ("why no entry this bar"); table may not exist yet.
        try:
            dec_rows = await conn.fetch("SELECT * FROM v2_paper_latest_decision")
        except Exception:
            dec_rows = []
    by = {r["strategy"]: r for r in rows}
    decisions = {r["strategy"]: record_to_dict(r) for r in dec_rows}

    def strat(name: str) -> dict:
        r = by.get(name)
        closed = int(r["closed_trades"]) if r else 0
        wins = int(r["wins"]) if r else 0
        return {
            "strategy": name,
            "open_trades": int(r["open_trades"]) if r else 0,
            "closed_trades": closed,
            "realized_pnl": float(r["realized_pnl"]) if r else 0.0,
            "win_rate": round(wins / closed, 3) if closed else None,
            "last_decision": decisions.get(name),   # {action, reason, step_reached, state_4h, ...}
        }

    return {
        "symbol": sym,
        "strategy_1": strat("strategy_1"),
        "strategy_2": strat("strategy_2"),
        "current_state": latest["state"] if latest else None,
        "latest_ts": latest["timestamp"].isoformat() if latest else None,
        "total_bars": bars,
    }


@router.get("/sel/phase/current")
async def phase_current():
    p = await get_pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM v2_strategy_phase_history ORDER BY timestamp DESC LIMIT 1"
        )
        return record_to_dict(row) or {}

@router.get("/sel/decision-trail/recent")
async def decision_trail_recent(limit: int = 20):
    p = await get_pool()
    async with p.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM v2_decision_trail ORDER BY timestamp DESC LIMIT $1", limit
        )
        return [record_to_dict(r) for r in rows]


@router.get("/sel/decision-trail/full")
async def decision_trail_full(symbol: str = Query("BTC-USDT"), limit: int = 50):
    """Read surface for the rich per-bar decision trail (`sel_decision_trail`): the full WHY
    of each decision — feature snapshot, state + reason, proposed vs final action, the matched
    rule, risk veto + details, fill, config_hash. This is the Helios differentiator that was
    persisted but had no read API (audit P1-7), so operators couldn't see it.

    Returns [] (not an error) when the table doesn't exist yet, so the UI degrades gracefully
    before the paper engine has written any trail."""
    p = await get_pool()
    async with p.acquire() as conn:
        try:
            rows = await conn.fetch(
                "SELECT * FROM sel_decision_trail WHERE symbol=$1 ORDER BY time DESC LIMIT $2",
                normalize_symbol(symbol), limit,
            )
        except Exception:
            return []
        return [record_to_dict(r) for r in rows]
