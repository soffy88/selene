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
    current_state: Optional[str] = None
    current_state_dwell_bars: Optional[int] = (
        None  # 4H bars the current state has held (Wave S2C)
    )


def normalize_symbol(symbol: str) -> str:
    if symbol == "BTCUSDT":
        return "BTC-USDT"
    if symbol == "ETHUSDT":
        return "ETH-USDT"
    return symbol


def record_to_dict(record):
    if not record:
        return None
    d = dict(record)
    for k, v in d.items():
        if hasattr(v, "quantize"):
            d[k] = float(v)
        elif isinstance(v, datetime):
            d[k] = v.isoformat()
    return d


@router.get("/sel/state/health", response_model=StateHealth)
async def state_health(symbol: str = Query("BTC-USDT")):
    symbol = normalize_symbol(symbol)
    p = await get_pool()
    async with p.acquire() as conn:
        count = await conn.fetchval(
            "SELECT count(*) FROM v2_bars_4h WHERE symbol=$1", symbol
        )
        latest = await conn.fetchval(
            "SELECT max(time) FROM v2_bars_4h WHERE symbol=$1", symbol
        )
        sc_rows = await conn.fetch(
            "SELECT state, count(*)::int AS cnt FROM v2_state_history "
            "WHERE timestamp > NOW() - INTERVAL '24 hours' GROUP BY state"
        )
        warn_count = await conn.fetchval(
            "SELECT count(*) FROM v2_state_history "
            "WHERE timestamp > NOW() - INTERVAL '30 days' AND state IS NULL"
        )
        # Current-state dwell (Wave S2C): how many consecutive most-recent 4H bars share the
        # latest state. Counted from the recent tail rather than trusting duration_4h, so a
        # gap/backfill can't inflate it.
        dwell_rows = await conn.fetch(
            "SELECT state FROM v2_state_history ORDER BY timestamp DESC LIMIT 1000"
        )

        count = count or 0
        satiation = min(count / 180 * 100, 100.0)
        is_ready = count >= 180
        state_counts = {r["state"]: r["cnt"] for r in sc_rows if r["state"]}

        current_state = dwell_rows[0]["state"] if dwell_rows else None
        current_state_dwell_bars = None
        if current_state is not None:
            current_state_dwell_bars = 0
            for r in dwell_rows:
                if r["state"] == current_state:
                    current_state_dwell_bars += 1
                else:
                    break

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
            current_state=current_state,
            current_state_dwell_bars=current_state_dwell_bars,
        )


@router.get("/sel/state/history")
async def state_history(
    symbol: str = Query("BTC-USDT"), hours: int = 24, limit: int = 100
):
    """Recent regime states with the fields that actually exist in v2_state_history:
    the state, where it transitioned FROM, the transition trigger (Release/Stress/Exhaustion),
    how long it has held (duration_4h), feature completeness (computed from state_features),
    and cold-start. The old 'direction'/'confidence' columns had no backing data — the state
    machine is deterministic and never populated sub_state — so they are dropped."""
    symbol = normalize_symbol(symbol)
    p = await get_pool()
    async with p.acquire() as conn:
        rows = await conn.fetch(
            "SELECT h.timestamp, h.state, h.transition_from, h.transition_via, h.duration_4h, "
            "  h.state_features, d.action AS s1_action, d.step_reached AS s1_step, d.reason AS s1_reason "
            "FROM v2_state_history h "
            "LEFT JOIN v2_strategy_decision d "
            "  ON d.timestamp = h.timestamp AND d.strategy = 'strategy_1' "
            "ORDER BY h.timestamp DESC LIMIT $1",
            limit,
        )
    out = []
    for r in rows:
        f = r["state_features"]
        if isinstance(f, str):
            f = json.loads(f)
        f = f or {}
        total = len(f)
        non_null = sum(1 for v in f.values() if v is not None)
        out.append(
            {
                "time": r["timestamp"].isoformat(),
                "state": r["state"],
                "transition_from": r["transition_from"],
                "transition_via": r["transition_via"],
                "duration_4h": r["duration_4h"],
                "feature_completeness": round(non_null / total, 3) if total else None,
                "cold_start": bool(f.get("cold_start")) if "cold_start" in f else None,
                # per-bar S1 decision (#2): what Strategy 1 decided on this bar and which step blocked
                "s1_action": r["s1_action"],
                "s1_step": r["s1_step"],
                "s1_reason": r["s1_reason"],
            }
        )
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
            "ORDER BY b.time DESC LIMIT $2",
            sym,
            bars,
        )
    return [
        {
            "time": int(r["time"].timestamp()),
            "open": float(r["open"]),
            "high": float(r["high"]),
            "low": float(r["low"]),
            "close": float(r["close"]),
            "state": r["state"],
        }
        for r in reversed(rows)
    ]


@router.get("/sel/observations")
async def sel_observations():
    """Latest reading of each observation-only tool (HMM regime / HMM boundary / TDA clustering /
    permutation entropy / transfer entropy / wavelet / Hawkes cascade). These never touch trading
    — they're a second, independent read on the market. Graceful [] until populated."""
    _NAMES = {
        "B1": "HMM 状态分歧",
        "B2": "HMM 边界套利",
        "TDA2": "TDA 聚类分歧",
        "I1": "排列熵(随机度)",
        "T2": "转移熵(资金费→价)",
        "W2": "小波多重分形",
        "H3": "Hawkes 级联预警",
        # v2.2 lens batch (observation-only; offline gate: lens_verdict_v1)
        "CHAN2": "缠论 背驰",
        "CHAN3": "缠论 中枢重叠",
        "ICT2": "ICT Swing 结构",
        "ICT1": "ICT VPIN 毒性流",
    }
    p = await get_pool()
    async with p.acquire() as conn:
        try:
            rows = await conn.fetch(
                "SELECT * FROM v2_observation_latest ORDER BY tool_id"
            )
        except Exception:
            return []
    out = []
    for r in rows:
        d = record_to_dict(r)
        d["name"] = _NAMES.get(r["tool_id"], r["tool_id"])
        out.append(d)
    return out


@router.get("/sel/lens/events")
async def sel_lens_events(bars: int = 300):
    """Chan/ICT lens events (v2.2 batch) for the SEL chart overlay — chan_pivot /
    chan_divergence / swing_structure / vpin rows from v2_inverse_vocab_events over
    the chart's window. `time` is unix seconds FLOORED TO THE 4H BAR OPEN (bar-tool
    events already carry bar-open timestamps; vpin buckets close intra-bar and are
    floored so markers land on chart bars). label comes from tool_metadata.
    Graceful [] until populated."""
    hours = bars * 4
    p = await get_pool()
    async with p.acquire() as conn:
        try:
            rows = await conn.fetch(
                "SELECT timestamp, vocab, intensity, associated_state, tool_metadata "
                "FROM v2_inverse_vocab_events "
                "WHERE vocab IN ('chan_pivot','chan_divergence','swing_structure','vpin') "
                "AND timestamp >= NOW() - make_interval(hours => $1) "
                "ORDER BY timestamp",
                hours,
            )
        except Exception:
            return []
    import json as _json

    out = []
    for r in rows:
        meta = r["tool_metadata"]
        if isinstance(meta, str):
            try:
                meta = _json.loads(meta)
            except Exception:
                meta = {}
        out.append(
            {
                "time": int(r["timestamp"].timestamp() // 14400 * 14400),
                "vocab": r["vocab"],
                "label": (meta or {}).get("label") or (meta or {}).get("event"),
                "value": float(r["intensity"]) if r["intensity"] is not None else None,
                "state": r["associated_state"],
            }
        )
    return out


@router.get("/sel/counterfactual")
async def sel_counterfactual():
    """Counterfactual S1/S2 entries over the full price history, computed by ASSUMING the
    unavailable derivative gates (OI/entropy/funding) pass — i.e. where the price+σ+CUSUM
    structure WOULD allow an entry. This is an upper-bound exploration, NOT a validated
    backtest (no DSR deflation, no out-of-sample split). Empty until the scan has populated
    v2_counterfactual_trades; the table may not exist yet (graceful)."""
    p = await get_pool()
    async with p.acquire() as conn:
        try:
            rows = await conn.fetch(
                "SELECT strategy, direction, entry_time, entry_price, exit_time, exit_price, "
                "pnl_usdt, exit_reason FROM v2_counterfactual_trades ORDER BY entry_time"
            )
        except Exception:
            return {"trades": [], "summary": None}
    trades = [
        {
            "strategy": r["strategy"],
            "direction": r["direction"],
            "entry_time": int(r["entry_time"].timestamp()),
            "entry_price": float(r["entry_price"]),
            "exit_time": int(r["exit_time"].timestamp()) if r["exit_time"] else None,
            "exit_price": float(r["exit_price"])
            if r["exit_price"] is not None
            else None,
            "pnl_usdt": float(r["pnl_usdt"]) if r["pnl_usdt"] is not None else None,
            "exit_reason": r["exit_reason"],
        }
        for r in rows
    ]
    closed = [t for t in trades if t["pnl_usdt"] is not None]
    wins = sum(1 for t in closed if t["pnl_usdt"] > 0)
    summary = (
        {
            "n_trades": len(trades),
            "win_rate": round(wins / len(closed), 3) if closed else None,
            "net_pnl_usdt": round(sum(t["pnl_usdt"] for t in closed), 2),
        }
        if trades
        else None
    )
    return {"trades": trades, "summary": summary}


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
            "SELECT * FROM v2_trades WHERE exit_time IS NOT NULL ORDER BY exit_time DESC LIMIT $1",
            limit,
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
            "FROM v2_trades GROUP BY strategy"
        )
        latest = await conn.fetchrow(
            "SELECT state, timestamp FROM v2_state_history ORDER BY timestamp DESC LIMIT 1"
        )
        bars = await conn.fetchval(
            "SELECT count(*) FROM v2_bars_4h WHERE symbol = $1", sym
        )
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
            "last_decision": decisions.get(
                name
            ),  # {action, reason, step_reached, state_4h, ...}
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
                normalize_symbol(symbol),
                limit,
            )
        except Exception:
            return []
        return [record_to_dict(r) for r in rows]
