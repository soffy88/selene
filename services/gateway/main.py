"""
CryptoWatch v4 — API Gateway
Single public-facing FastAPI service. All frontend/CLI traffic goes here.
Aggregates data from Redis cache (set by individual services).
WebSocket endpoint for real-time push to frontend.
"""
import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from shared.db.redis_client import init_redis, get_redis, hgetall_json, get_json
from shared.events.streams import (
    STREAM_SIGNAL_SCORED, STREAM_ORDER_LIFECYCLE, STREAM_PORTFOLIO_STATE,
    STREAM_SYSTEM_ALERTS, STREAM_ONCHAIN_EVENTS, CG_GATEWAY, consume, StreamEvent
)

logger = logging.getLogger(__name__)

# ── WebSocket manager ─────────────────────────────────────────────────────────
class WSManager:
    def __init__(self):
        self._clients: set[WebSocket] = set()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self._clients.add(ws)

    def disconnect(self, ws: WebSocket):
        self._clients.discard(ws)

    async def broadcast(self, msg: dict):
        dead = set()
        text = json.dumps(msg)
        for ws in self._clients:
            try:
                await ws.send_text(text)
            except Exception:
                dead.add(ws)
        for ws in dead:
            self._clients.discard(ws)

    @property
    def count(self) -> int:
        return len(self._clients)


ws_manager = WSManager()


# ── Stream consumers (push to WebSocket clients) ──────────────────────────────
async def _on_signal(event: StreamEvent):
    await ws_manager.broadcast({"type": "signal", "data": event.data})

async def _on_order(event: StreamEvent):
    await ws_manager.broadcast({"type": "order", "data": event.data})

async def _on_portfolio(event: StreamEvent):
    await ws_manager.broadcast({"type": "portfolio", "data": event.data})

async def _on_alert(event: StreamEvent):
    await ws_manager.broadcast({"type": "alert", "data": event.data})

async def _on_onchain(event: StreamEvent):
    """实时链上事件推送到前端 WebSocket"""
    await ws_manager.broadcast({"type": "onchain", "data": event.data})


# ── App lifespan ──────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    import os
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    init_redis(redis_url)
    logger.info("Gateway: Redis connected")

    # Start stream consumers for real-time WS push
    tasks = [
        asyncio.create_task(consume(STREAM_SIGNAL_SCORED,   CG_GATEWAY, "gw-signals",   _on_signal)),
        asyncio.create_task(consume(STREAM_ORDER_LIFECYCLE, CG_GATEWAY, "gw-orders",    _on_order)),
        asyncio.create_task(consume(STREAM_PORTFOLIO_STATE, CG_GATEWAY, "gw-portfolio", _on_portfolio)),
        asyncio.create_task(consume(STREAM_SYSTEM_ALERTS,   CG_GATEWAY, "gw-alerts",    _on_alert)),
        asyncio.create_task(consume(STREAM_ONCHAIN_EVENTS,  CG_GATEWAY, "gw-onchain",   _on_onchain)),
    ]
    logger.info("Gateway: ready")
    yield
    for t in tasks:
        t.cancel()


# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="CryptoWatch v4 Gateway", version="4.0.0", lifespan=lifespan)

# CORS: tightened off "*" (item #21). Set GATEWAY_CORS_ORIGINS (comma-separated)
# to your dashboard origin(s); defaults to localhost only.
_cors_origins = [o.strip() for o in os.getenv(
    "GATEWAY_CORS_ORIGINS", "http://localhost,http://localhost:80").split(",") if o.strip()]
app.add_middleware(CORSMiddleware, allow_origins=_cors_origins,
                   allow_methods=["*"], allow_headers=["*"])


# ── Auth for state-changing routes (item #21) ───────────────────────────────────
# Enforced only when GATEWAY_API_KEY is set, so dev stays open but a deployment
# locks down trade-affecting endpoints by setting the env var.
GATEWAY_API_KEY = os.getenv("GATEWAY_API_KEY", "")


async def require_api_key(x_api_key: str = Header(default="")):
    if GATEWAY_API_KEY and x_api_key != GATEWAY_API_KEY:
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")
    return True

try:
    from sel_engine.paper_interface.api import router as sel_router
    app.include_router(sel_router, prefix="/api/v4")
except ImportError:
    logger.warning("sel_engine not available — /api/v4/sel/* endpoints disabled")

# sel_v2 paper API (new)
try:
    from sel_v2.paper_interface.api import router as sel_v2_router
    if sel_v2_router is not None:
        app.include_router(sel_v2_router, prefix="/api/v2")
        logger.info("sel_v2 paper API enabled at /api/v2/sel/*")
except ImportError as e:
    logger.warning(f"sel_v2 paper API unavailable: {e}")


# ── Health ─────────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    from shared.db.redis_client import health_check
    redis_ok = await health_check()
    return {
        "status": "ok" if redis_ok else "degraded",
        "version": "4.0.0",
        "redis": redis_ok,
        "ws_clients": ws_manager.count,
        "timestamp": datetime.utcnow().isoformat(),
    }


# ── Prometheus metrics (item #12) ───────────────────────────────────────────────
@app.get("/metrics")
async def metrics():
    """Prometheus exposition of key system gauges derived from Redis. Real metrics
    (no fake data); a Prometheus server scrapes this once added to compose."""
    from fastapi.responses import PlainTextResponse
    from shared.metrics import render_prometheus
    from shared.db.redis_client import health_check

    out = []
    redis_ok = await health_check()
    out.append({"name": "selene_up", "value": 1, "help": "gateway process is up"})
    out.append({"name": "selene_redis_up", "value": redis_ok, "help": "redis reachable"})
    out.append({"name": "selene_ws_clients", "value": ws_manager.count,
                "help": "connected websocket clients"})
    try:
        pf = await get_json("cw4:portfolio:state") or {}
        if "total_equity" in pf:
            out.append({"name": "selene_portfolio_equity_usd", "value": pf.get("total_equity", 0),
                        "help": "portfolio mark-to-market equity"})
        if "current_drawdown" in pf:
            out.append({"name": "selene_portfolio_drawdown", "value": pf.get("current_drawdown", 0),
                        "help": "current drawdown fraction"})
        if "position_count" in pf:
            out.append({"name": "selene_open_positions", "value": pf.get("position_count", 0),
                        "help": "open position count"})
        risk = await get_json("cw4:risk:status") or {}
        if "daily_pnl" in risk:
            out.append({"name": "selene_daily_pnl_usd", "value": risk.get("daily_pnl", 0),
                        "help": "realized daily PnL"})
        r = get_redis()
        halted = bool(await r.get("cw4:execution:halt"))
        out.append({"name": "selene_execution_halted", "value": halted,
                    "help": "1 if execution halt is engaged"})
    except Exception as e:
        logger.warning(f"/metrics partial: {e}")
    return PlainTextResponse(render_prometheus(out))


# ── Market ─────────────────────────────────────────────────────────────────────
@app.get("/api/v4/market")
async def market_snapshot():
    """Aggregate market snapshot from Redis cache."""
    prices    = await hgetall_json("cw4:prices")       or {}
    funding   = await hgetall_json("cw4:funding_rates") or {}
    oi        = await hgetall_json("cw4:oi")            or {}
    lsr       = await hgetall_json("cw4:lsr")           or {}
    return {"prices": prices, "funding_rates": funding, "oi": oi, "lsr": lsr,
            "symbol_count": len(prices)}


# ── Signals ─────────────────────────────────────────────────────────────────────
@app.get("/api/v4/signals")
async def list_signals(
    limit: int = Query(50, le=200),
    symbol: str = Query(None),
    signal_type: str = Query(None),
    regime: str = Query(None),
    min_probability: float = Query(0.0),
):
    """Read scored signals from Redis cache (set by Signal Service)."""
    raw = await hgetall_json("cw4:signals:recent") or {}
    signals = list(raw.values())
    signals.sort(key=lambda s: s.get("timestamp", ""), reverse=True)

    if symbol:        signals = [s for s in signals if s.get("symbol") == symbol.upper()]
    if signal_type:   signals = [s for s in signals if s.get("signal_type") == signal_type]
    if regime:        signals = [s for s in signals if s.get("regime") == regime]
    if min_probability > 0: signals = [s for s in signals if s.get("win_probability", 0) >= min_probability]

    return {"signals": signals[:limit], "total": len(signals)}


@app.get("/api/v4/signals/pending")
async def pending_signals():
    raw = await hgetall_json("cw4:signals:pending") or {}
    return {"pending": list(raw.values()), "count": len(raw)}


@app.post("/api/v4/signals/{signal_id}/confirm", dependencies=[Depends(require_api_key)])
async def confirm_signal(signal_id: str):
    r = get_redis()
    await r.hset("cw4:signals:confirmed", signal_id, json.dumps({"action": "confirm", "ts": datetime.utcnow().isoformat()}))
    await r.hdel("cw4:signals:pending", signal_id)
    return {"status": "confirmed", "id": signal_id}


@app.post("/api/v4/signals/{signal_id}/reject", dependencies=[Depends(require_api_key)])
async def reject_signal(signal_id: str):
    r = get_redis()
    await r.hdel("cw4:signals:pending", signal_id)
    return {"status": "rejected", "id": signal_id}


# ── Portfolio ───────────────────────────────────────────────────────────────────
@app.get("/api/v4/portfolio/state")
async def portfolio_state():
    state = await get_json("cw4:portfolio:state")
    if not state:
        raise HTTPException(503, "Portfolio state not yet available")
    return state


@app.get("/api/v4/portfolio/pnl")
async def portfolio_pnl(period: str = Query("7d")):
    pnl = await get_json(f"cw4:portfolio:pnl:{period}") or {}
    return {"period": period, "pnl": pnl}


# ── Regime ──────────────────────────────────────────────────────────────────────
@app.get("/api/v4/regime")
async def regime_status():
    try:
        regimes = await hgetall_json("cw4:regimes") or {}
        return {"regimes": regimes, "symbol_count": len(regimes)}
    except Exception as exc:
        import logging; logging.getLogger(__name__).error("regime_status failed: %s", exc)
        return {"regimes": {}, "symbol_count": 0, "error": str(exc)}


@app.get("/api/v4/regime/current")
async def current_regime():
    """Aggregate regime distribution across all tracked symbols."""
    regimes = await hgetall_json("cw4:regimes") or {}
    distribution: dict[str, int] = {}
    for v in regimes.values():
        r = v.get("regime", "UNKNOWN") if isinstance(v, dict) else str(v)
        distribution[r] = distribution.get(r, 0) + 1
    dominant = max(distribution, key=distribution.get) if distribution else "UNKNOWN"
    return {"distribution": distribution, "dominant": dominant, "total_symbols": len(regimes)}


# ── Risk ────────────────────────────────────────────────────────────────────────
@app.get("/api/v4/risk/status")
async def risk_status():
    status = await get_json("cw4:risk:status")
    if not status:
        return {"status": "initializing"}
    return status


# NOTE (item #21): the duplicate simple reset route that lived here was removed.
# FastAPI keeps the first registration for a path, so it shadowed the richer
# circuit_breaker_reset() below (which calls risk-service + writes a PG audit).
# That richer handler is now the single active reset route.


# ── Execution / Orders ──────────────────────────────────────────────────────────
async def _scan_orders_stripped() -> list[dict]:
    """HSCAN cw4:orders:recent in chunks, stripping 'history' to avoid OOM (38 MB hash)."""
    r = get_redis()
    _STRIP = {"history"}
    orders: list[dict] = []
    cursor = 0
    while True:
        cursor, items = await r.hscan("cw4:orders:recent", cursor=cursor, count=20)
        for v in items.values():
            try:
                o = json.loads(v)
                if isinstance(o, dict):
                    orders.append({k: val for k, val in o.items() if k not in _STRIP})
            except Exception:
                pass
        if cursor == 0:
            break
    return orders


@app.get("/api/v4/orders")
async def list_orders(limit: int = Query(50, le=200), state: str = Query(None)):
    try:
        orders = await _scan_orders_stripped()
        orders.sort(key=lambda o: o.get("created_at", ""), reverse=True)
        if state:
            orders = [o for o in orders if o.get("state") == state]
        return {"orders": orders[:limit], "total": len(orders)}
    except Exception as exc:
        logger.error("list_orders failed: %s", exc)
        return {"orders": [], "total": 0, "error": str(exc)}


@app.get("/api/v4/execution/slippage/{symbol}")
async def slippage_estimate(symbol: str, notional: float = Query(10000)):
    from services.execution.slippage.model import SlippageModel
    sm = SlippageModel()
    prices = await hgetall_json("cw4:prices") or {}
    price  = prices.get(symbol.upper(), {})
    if isinstance(price, dict):
        price = price.get("price", 0)
    # Rough estimate without live order book
    est = sm.estimate(notional, 0.80, 50_000_000, 0.02, "MARKET")
    return {"symbol": symbol, "notional_usd": notional, "estimate": {
        "spread_pct": est.spread, "impact_pct": est.impact,
        "timing_pct": est.timing, "total_pct": est.total,
        "total_usd": est.total_usd, "recommendation": est.recommendation,
    }}


# ── Funding Arbitrage ────────────────────────────────────────────────────────────
@app.get("/api/v4/funding/opportunities")
async def funding_opportunities():
    opps = await get_json("cw4:funding:opportunities") or []
    return {"opportunities": opps, "count": len(opps)}


@app.get("/api/v4/funding/positions")
async def funding_positions():
    positions = await hgetall_json("cw4:funding:positions") or {}
    return {"positions": list(positions.values()), "count": len(positions)}


@app.post("/api/v4/funding/execute/{symbol}", dependencies=[Depends(require_api_key)])
async def execute_funding_arb(symbol: str):
    r = get_redis()
    await r.publish("cw4:commands", json.dumps({"cmd": "open_funding_arb", "symbol": symbol}))
    return {"status": "command_sent", "symbol": symbol}


# ── Backtest ────────────────────────────────────────────────────────────────────
_backtest_results: dict = {}
_backtest_running = False


@app.get("/api/v4/backtest/wfo")
async def run_wfo(
    background_tasks=None,
    symbols: str = Query("BTCUSDT,ETHUSDT"),
    interval: str = Query("1h"),
    days_back: int = Query(180, le=365),
):
    import uuid
    from fastapi import BackgroundTasks
    global _backtest_running
    if _backtest_running:
        return {"status": "already_running"}

    run_id = uuid.uuid4().hex[:8]

    async def run():
        global _backtest_running
        _backtest_running = True
        try:
            from backtest.engine import WFOEngine, WFOConfig
            from services.data.ingestion.market_provider import MarketDataRESTClient
            engine = WFOEngine(WFOConfig(train_days=90, test_days=30))
            client = MarketDataRESTClient()
            results = {}
            for sym in [s.strip().upper() for s in symbols.split(",")]:
                candles = await client.fetch_klines(sym, interval, limit=days_back * 24)
                funding = await client.fetch_funding_history(sym)
                results[sym] = (await engine.run(sym, candles, funding)).to_dict()
            _backtest_results[run_id] = results
        finally:
            _backtest_running = False

    asyncio.create_task(run())
    return {"status": "started", "run_id": run_id, "symbols": symbols}


@app.get("/api/v4/backtest/wfo/{run_id}")
async def get_wfo_results(run_id: str):
    if run_id not in _backtest_results:
        return {"status": "running" if _backtest_running else "not_found"}
    return {"status": "completed", "results": _backtest_results[run_id]}


# ── Moonshots ─────────────────────────────────────────────────────────────────
@app.get("/api/v4/moonshots")
async def moonshots(limit: int = Query(20, le=100)):
    raw = await get_json("cw4:moonshots") or []
    return {"moonshots": raw[:limit], "total": len(raw)}


# ── Onchain Sentinel (v4.1) ────────────────────────────────────────────────────

@app.get("/api/v4/onchain/state")
async def onchain_state():
    """所有 symbol 的链上状态快照（评分、regime调整、历史事件）"""
    from services.signal.factors.composite import get_onchain_composite
    result = {}
    for sym in ["BTCUSDT", "ETHUSDT", "SOLUSDT"]:
        result[sym] = await get_onchain_composite(sym)
    return {"onchain": result, "ts": datetime.utcnow().isoformat()}


@app.get("/api/v4/onchain/state/{symbol}")
async def onchain_state_symbol(symbol: str):
    """单个 symbol 的链上状态"""
    from services.signal.factors.composite import get_onchain_composite
    state = await get_onchain_composite(symbol.upper())
    if not state:
        raise HTTPException(404, f"No onchain data for {symbol}")
    return state


@app.get("/api/v4/onchain/stats/{symbol}")
async def onchain_stats(
    symbol: str,
    signal_class: str = Query("whale_inflow_exchange"),
    window: int = Query(24, description="回溯窗口（小时）"),
    lookback: int = Query(90, description="历史天数"),
):
    """
    链上信号历史胜率查询。
    例：过去90天 BTC 鲸鱼流入后24h 的胜率和均值收益。
    """
    from services.onchain.historian import get_signal_stats
    return get_signal_stats(symbol.upper(), signal_class, window, lookback)


@app.get("/api/v4/onchain/wallet/{address}")
async def onchain_wallet_stats(address: str, chain: str = Query("ETH")):
    """聪明钱包历史行为画像"""
    from services.onchain.historian import get_wallet_stats
    return get_wallet_stats(address, chain.upper())


@app.get("/api/v4/onchain/summary/{symbol}")
async def onchain_summary(symbol: str, days: int = Query(7)):
    """链上活跃度日度汇总"""
    from services.onchain.historian import get_daily_summary
    return {"symbol": symbol.upper(), "summary": get_daily_summary(symbol.upper(), days)}


# ── Monitoring Service (v4.1) ──────────────────────────────────────────────────

@app.get("/api/v4/monitor/health")
async def monitor_health():
    """监控服务状态"""
    r = get_redis()
    last = await r.get("cw4:monitor:last_run_date") or "从未运行"
    raw  = await r.get("cw4:monitor:trend")
    trend_days = len(json.loads(raw)) if raw else 0
    return {"last_report": last, "trend_days": trend_days}


@app.get("/api/v4/monitor/report")
async def monitor_report():
    """获取最新简报完整数据"""
    r   = get_redis()
    raw = await r.get("cw4:monitor:latest_report")
    if not raw:
        return {"status": "no_data",
                "msg": "尚未生成报告，POST /api/v4/monitor/trigger 立即生成"}
    return json.loads(raw)


@app.post("/api/v4/monitor/trigger", dependencies=[Depends(require_api_key)])
async def monitor_trigger(days: int = Query(1)):
    """手动触发立即生成简报"""
    import asyncio
    # 异步调用 monitoring-service 的内部 HTTP
    import aiohttp
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                f"http://monitoring-service:8021/report/now?days={days}",
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                return await resp.json()
    except Exception as e:
        return {"status": "error", "msg": str(e),
                "hint": "确认 monitoring-service 已启动"}


@app.get("/api/v4/monitor/mode-thresholds")
@app.get("/api/v4/monitor/recommendation")   # deprecated alias (Helios observe-only: prefer /mode-thresholds)
async def monitor_mode_thresholds():
    """返回模式切换门槛的【观察状态】（不是建议；Dashboard 首页展示用）。"""
    r   = get_redis()
    raw = await r.get("cw4:monitor:latest_report")
    if not raw:
        return {"status": "no_data"}
    data = json.loads(raw)
    rec = data.get("data", {}).get("recommendation", {})
    return {
        "mode_thresholds": rec,
        "recommendation":  rec,   # deprecated key kept for back-compat
        "trend":           data.get("trend", {}),
        "generated_at":    data.get("generated_at"),
    }


@app.get("/api/v4/monitor/trend")
async def monitor_trend():
    """IC 历史趋势（最近30天）"""
    r   = get_redis()
    raw = await r.get("cw4:monitor:trend")
    if not raw:
        return {"trend": [], "analysis": {}}
    trend = json.loads(raw)
    # 简单趋势分析
    recent = [t["mean_ic"] for t in trend[-7:] if t.get("mean_ic")]
    avg    = round(sum(recent)/len(recent), 4) if recent else None
    return {
        "trend":      trend[-30:],
        "total_days": len(trend),
        "avg_ic_7d":  avg,
    }



# ── Regime / HMM / Weights (v4.1) ─────────────────────────────────────────────

@app.get("/api/v4/regime/hmm/{symbol}")
async def regime_hmm(symbol: str):
    """HMM 慢信号 Regime（每6小时更新）"""
    r = get_redis()
    raw = await r.get(f"cw4:regime:hmm:{symbol.upper()}")
    if not raw:
        return {"status": "no_data", "symbol": symbol.upper(),
                "msg": "HMM 尚未完成首次训练（需要 ≥60 条 1h K线）"}
    return json.loads(raw)


@app.get("/api/v4/regime/fused")
async def regime_fused_all():
    """所有 symbol 的融合 Regime 状态（HMM × ADX+ATR）"""
    import aiohttp
    try:
        async with aiohttp.ClientSession() as s:
            results = {}
            for sym in ["BTCUSDT", "ETHUSDT", "SOLUSDT"]:
                async with s.get(
                    f"http://signal-service:8002/regime/fused/{sym}",
                    timeout=aiohttp.ClientTimeout(total=3),
                ) as r:
                    if r.ok:
                        results[sym] = await r.json()
            return {"fused_regimes": results, "ts": datetime.utcnow().isoformat()}
    except Exception as e:
        return {"error": str(e), "hint": "signal-service 需要先启动"}


@app.get("/api/v4/signals/weights")
async def signal_weights():
    """EWMA 学习到的当前动态因子权重 + 准确率"""
    import aiohttp
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                "http://signal-service:8002/weights",
                timeout=aiohttp.ClientTimeout(total=3),
            ) as r:
                return await r.json()
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/v4/risk/circuit-breaker")
async def circuit_breaker_status():
    """熔断器持久化状态（重启后不丢失）"""
    r = get_redis()
    raw_state  = await r.get("cw4:circuit_breaker:state")
    raw_ts     = await r.get("cw4:circuit_breaker:open_ts")
    raw_reason = await r.get("cw4:circuit_breaker:reason")

    def dec(v):
        return (v.decode() if isinstance(v, bytes) else v) if v else None

    state  = dec(raw_state)  or "CLOSED"
    ts     = dec(raw_ts)     or ""
    reason = dec(raw_reason) or ""

    return {
        "state":         state,
        "is_open":       state.upper() == "OPEN",
        "open_ts":       ts,
        "reason":        reason,
        "reset_command": "POST /api/v4/risk/circuit-breaker/reset",
    }


@app.post("/api/v4/risk/circuit-breaker/reset", dependencies=[Depends(require_api_key)])
async def circuit_breaker_reset():
    """手动解除熔断（同时清除 Redis + 写 PG 审计）"""
    import aiohttp
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                "http://risk-service:8004/circuit-breaker/reset",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as r:
                return await r.json()
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/v4/system/overview")
async def system_overview():
    """
    系统总览：一次性返回所有关键状态
    适合 Dashboard 首屏加载
    """
    r = get_redis()

    async def safe_get(key):
        try:
            raw = await r.get(key)
            return json.loads(raw) if raw else None
        except Exception:
            return None

    async def safe_hlen(key):
        try:
            return await r.hlen(key)
        except Exception:
            return 0

    # 并发读取所有状态
    portfolio, risk, monitor, regimes = await asyncio.gather(
        safe_get("cw4:portfolio:state"),
        safe_get("cw4:risk:status"),
        safe_get("cw4:monitor:latest_report"),
        r.hgetall("cw4:regimes"),
    )

    # Regime 分布统计
    regime_dist = {}
    if regimes:
        for v in regimes.values():
            try:
                d = json.loads(v.decode() if isinstance(v, bytes) else v)
                rg = d.get("regime", "UNKNOWN")
                regime_dist[rg] = regime_dist.get(rg, 0) + 1
            except Exception:
                pass

    # 熔断状态
    cb_raw = await r.get("cw4:circuit_breaker:state")
    cb_state = (cb_raw.decode() if isinstance(cb_raw, bytes) else cb_raw) or "CLOSED"

    # 监控建议（只取关键字段）
    recommendation = {}
    if monitor:
        rec = monitor.get("data", {}).get("recommendation", {})
        recommendation = {
            "action":     rec.get("action", ""),
            "confidence": rec.get("confidence", ""),
            "next_mode":  rec.get("next_mode", ""),
            "blockers":   len(rec.get("blockers", [])),
        }

    import os
    return {
        "ts": datetime.utcnow().isoformat(),
        "exec_mode": os.environ.get("EXEC_MODE", "PAPER"),
        "portfolio": {
            "equity":    portfolio.get("total_equity")    if portfolio else None,
            "daily_pnl": portfolio.get("daily_pnl")       if portfolio else None,
            "positions": portfolio.get("position_count")  if portfolio else 0,
            "drawdown":  portfolio.get("current_drawdown") if portfolio else None,
        },
        "risk": {
            "circuit_broken": cb_state.upper() == "OPEN",
            "drawdown_level": risk.get("level") if risk else "UNKNOWN",
            "new_trades":     risk.get("new_trades_allowed", True) if risk else False,
        },
        "regimes": {
            "distribution": regime_dist,
            "dominant": max(regime_dist, key=regime_dist.get) if regime_dist else "UNKNOWN",
        },
        "monitor": recommendation,
        "signal_count": await safe_hlen("cw4:signals:recent"),
    }


# ── Config ────────────────────────────────────────────────────────────────────
@app.get("/api/v4/config/{module}")
async def get_config(module: str):
    import os
    import re
    prefix = module.upper() + "_"
    cfg = {k.replace(prefix, "").lower(): v
           for k, v in os.environ.items() if k.startswith(prefix)}
    return {"module": module, "config": cfg}


# ── WebSocket ─────────────────────────────────────────────────────────────────
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await ws_manager.connect(websocket)
    try:
        # Send current state on connect
        market_data = await market_snapshot()
        signals_resp = await list_signals(limit=20, symbol=None, signal_type=None, regime=None, min_probability=0.0)
        await websocket.send_json({
            "type": "init",
            "data": {
                "prices": market_data.get("prices", {}),
                "funding_rates": market_data.get("funding_rates", {}),
                "signals": signals_resp.get("signals", [])[:20],
            }
        })
        # Keep-alive loop
        while True:
            await asyncio.sleep(25)
            await websocket.send_json({"type": "ping", "ts": datetime.utcnow().isoformat()})
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
    except Exception as e:
        logger.error(f"WS error: {e}")
        ws_manager.disconnect(websocket)
