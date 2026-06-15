import os
import asyncpg
from fastapi import APIRouter, Query, HTTPException
from typing import List, Optional
from pydantic import BaseModel
from datetime import datetime

router = APIRouter()
pool = None

async def get_pool():
    global pool
    if pool is None:
        db_url = os.environ.get("DB_URL", "postgresql://selene_app:sel_app_2026@platform-postgres:5432/selene")
        pool = await asyncpg.create_pool(db_url)
    return pool

class StateHealth(BaseModel):
    symbol: str
    total_bars_4h: int
    cold_start_bars: int
    active_bars: int
    satiation_pct: float
    is_ready: bool
    latest_ts: Optional[datetime]

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
    p = await get_pool()
    async with p.acquire() as conn:
        count = await conn.fetchval("SELECT count(*) FROM v2_bars_4h WHERE symbol=$1", symbol)
        latest = await conn.fetchval("SELECT max(time) FROM v2_bars_4h WHERE symbol=$1", symbol)
        
        count = count or 0
        satiation = min(count / 180 * 100, 100.0)
        is_ready = count >= 180
        
        return StateHealth(
            symbol=symbol,
            total_bars_4h=count,
            cold_start_bars=count, 
            active_bars=count,
            satiation_pct=round(satiation, 2),
            is_ready=is_ready,
            latest_ts=latest
        )

@router.get("/sel/state/history")
async def state_history(symbol: str = Query("BTC-USDT"), hours: int = 24, limit: int = 100):
    p = await get_pool()
    async with p.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM v2_state_history ORDER BY timestamp DESC LIMIT $1", limit
        )
        return [record_to_dict(r) for r in rows]

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
