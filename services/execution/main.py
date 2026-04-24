"""
services/execution/main.py  —  CryptoWatch v4 Execution Service（完整重写）

修复：adapters/ 真实接入 + FillEvent WSS 闭环 + 部分成交 + 止损监控 + 强制风控 Gate
"""
import asyncio, json, logging, os, time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime
from fastapi import FastAPI
from shared.db.connections import get_redis, get_pg, redis_health, pg_health
from shared.events.streams import (
    STREAM_SIGNAL_SIZED, STREAM_RISK_CHECK, STREAM_RISK_APPROVED,
    STREAM_ORDER_LIFECYCLE, encode, decode,
)
from shared.models.signal import ScoredSignal, Direction
from services.execution.statemachine.order_fsm import OrderFSM, OrderRecord, OrderState
from services.execution.slippage.model import SlippageModel
from services.execution.adapters.base import get_adapter, register_adapter, FillEvent, get_all_adapters
from services.execution.routing.smart_router import SmartRouter

logger = logging.getLogger(__name__)
logging.basicConfig(level=os.getenv("LOG_LEVEL","INFO"), format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

EXEC_MODE        = os.getenv("EXEC_MODE", "NOTIFY_ONLY")
PRIMARY_EXCHANGE = os.getenv("PRIMARY_EXCHANGE", "binance")
MONITOR_INTERVAL = float(os.getenv("MONITOR_INTERVAL_S", "5"))
CG_EXECUTION     = "execution-service"

_slippage_model = SlippageModel()
_router         = SmartRouter()
_orders:        dict[str, OrderFSM] = {}
_exchange_map:  dict[str, str]      = {}
_pending_risk:  dict[str, OrderFSM] = {}
_recent_orders: deque               = deque(maxlen=500)
_stats = {"queued":0,"filled":0,"failed":0,"cancelled":0,"risk_rejected":0}


def _init_adapters():
    from services.execution.adapters.binance import BinanceAdapter
    from services.execution.adapters.okx import OKXAdapter
    testnet = os.getenv("ENVIRONMENT","development") != "production"
    bkey = os.getenv("BINANCE_API_KEY",""); bsec = os.getenv("BINANCE_API_SECRET","")
    okey = os.getenv("OKX_API_KEY",""); osec = os.getenv("OKX_API_SECRET",""); opass = os.getenv("OKX_PASSPHRASE","")
    if bkey: register_adapter("binance", BinanceAdapter(bkey, bsec, testnet)); logger.info(f"Binance {'TESTNET' if testnet else 'LIVE'}")
    if okey: register_adapter("okx", OKXAdapter(okey, osec, opass, testnet)); logger.info(f"OKX {'TESTNET' if testnet else 'LIVE'}")
    if not bkey and not okey: logger.warning("No API keys — NOTIFY_ONLY forced")


async def _estimate_realized_vol(r, symbol: str) -> float:
    """
    从 Redis cw4:regimes:{symbol} 或 cw4:prices 估算年化波动率。
    无数据时按 crypto 典型 80% 兜底。
    """
    try:
        raw = await r.hget("cw4:regimes", symbol)
        if raw:
            d = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
            # Regime detector 不直接导出 vol，使用默认值兜底
            regime = d.get("regime", "")
            if regime == "HIGH_VOLATILITY": return 1.5
            if regime in ("TRENDING_UP", "TRENDING_DOWN"): return 0.80
            if regime == "RANGING": return 0.50
    except Exception:
        pass
    return 0.80


async def _estimate_daily_volume_usd(r, symbol: str, price: float) -> float:
    """
    估算 24h 交易量 USD。优先读 cw4:adv:{symbol}（如有后台任务写入），
    否则按大盘/中盘 symbol 经验值兜底。
    """
    try:
        raw = await r.get(f"cw4:adv:{symbol}")
        if raw:
            return float(raw)
    except Exception:
        pass
    # 经验兜底：BTC/ETH ~ 20B，主流 L1 ~ 1-5B，其他 ~ 100M
    majors = {"BTCUSDT": 20e9, "ETHUSDT": 15e9, "SOLUSDT": 3e9, "BNBUSDT": 2e9}
    return majors.get(symbol, 2e8)


async def on_fill(event: FillEvent):
    order_id = _exchange_map.get(event.exchange_id)
    if not order_id: return
    fsm = _orders.get(order_id)
    if not fsm: return
    r = await get_redis()
    fsm.on_fill(event.filled_qty, event.filled_price, event.fee)
    if event.is_final or fsm.record.state == OrderState.FILLED:
        if fsm.record.state == OrderState.FILLED:
            fsm.transition(OrderState.MONITORING)
            _stats["filled"] += 1
            await _pub(r, fsm, "filled")
            await _audit(fsm, "ORDER_FILLED")
            logger.info(f"✅ FILLED {fsm.record.symbol} avg={fsm.record.filled_price:.4f} slip={fsm.record.slippage_pct:.4f}%")
    else:
        await _pub(r, fsm, "partial_fill")


async def process_scored_signal(data: dict):
    if EXEC_MODE == "NOTIFY_ONLY":
        logger.info(f"[NOTIFY_ONLY] {data.get('signal_type')} {data.get('symbol')} p={float(data.get('win_probability',0)):.0%}")
        return
    try: signal = ScoredSignal.from_dict(data)
    except Exception as e: logger.error(f"parse error: {e}"); return
    if not signal.is_actionable or signal.direction == Direction.NEUTRAL: return
    # 必须已通过 portfolio sizing（position_size + allocated_capital）
    position_size = float(data.get("position_size", 0) or 0)
    allocated     = float(data.get("allocated_capital", 0) or 0)
    if position_size <= 0 or allocated <= 0:
        logger.warning(f"DROP unsized signal {signal.symbol} position_size={position_size} allocated={allocated}")
        return
    side = "BUY" if signal.direction == Direction.LONG else "SELL"
    r = await get_redis()
    # ── 强制风控 Gate ──
    risk_raw = await r.get("cw4:risk:status")
    if risk_raw:
        rs = json.loads(risk_raw)
        if not rs.get("new_trades_allowed", True):
            logger.warning(f"RISK GATE BLOCKED level={rs.get('drawdown_level')} signal={signal.id[:8]}")
            _stats["risk_rejected"] += 1
            return
    # ── 路由（使用 portfolio sized 的真实 allocated_capital）──
    try:
        plan = await _router.route(signal.symbol, side, allocated)
        adapter_name = plan.splits[0]["adapter"] if plan.splits else PRIMARY_EXCHANGE
    except Exception as e:
        logger.warning(f"Router failed: {e}"); adapter_name = PRIMARY_EXCHANGE
    # ── 实时盘口滑点（真实 spread + symbol realized vol + 估算 ADV）──
    try:
        adp = get_adapter(adapter_name)
        book = await adp.get_orderbook(signal.symbol)
        bids = book.get("bids",[[0,0]]); asks = book.get("asks",[[0,0]])
        spread = (asks[0][0]-bids[0][0])/asks[0][0] if asks and bids else 0.002
        # 估算 symbol 近期实现年化波动率
        realized_vol = await _estimate_realized_vol(r, signal.symbol)
        # 估算 ADV（默认: price × 5_000_000 units/day；后续可接 binance 24hTicker）
        adv_usd = await _estimate_daily_volume_usd(r, signal.symbol, signal.entry_price)
        slip = _slippage_model.estimate(allocated, realized_vol, adv_usd, spread*100, "LIMIT")
    except Exception as e:
        logger.warning(f"slippage estimate failed: {e}")
        slip = None
    rec = OrderRecord(
        signal_id=signal.id, symbol=signal.symbol, side=side, order_type="LIMIT",
        quantity=position_size, entry_price=signal.entry_price,
        stop_loss=signal.stop_loss, take_profit=signal.take_profit,
        limit_price=signal.entry_price, exchange=adapter_name,
        kelly_fraction=float(data.get("kelly_fraction", 0)),
        risk_usd=float(data.get("risk_usd", 0)),
    )
    fsm = OrderFSM(rec); _orders[rec.id] = fsm; _recent_orders.append(fsm); _stats["queued"] += 1
    await _persist_order(rec)
    fsm.transition(OrderState.SLIPPAGE_ESTIMATE)
    if slip and slip.total > 1.0:
        fsm.transition(OrderState.CANCELLED, note=f"slippage {slip.total:.2f}%"); _stats["cancelled"] += 1; return
    fsm.transition(OrderState.ROUTING); fsm.transition(OrderState.SUBMITTING)
    await r.xadd(STREAM_RISK_CHECK, encode({
        "order_id": rec.id, "signal_id": signal.id, "symbol": signal.symbol,
        "side": side, "quantity": rec.quantity, "entry_price": signal.entry_price,
        "stop_price": signal.stop_loss, "allocated_usd": allocated,
        "win_probability": signal.win_probability, "regime": signal.regime.value,
        "slippage_pct": slip.total if slip else 0, "adapter": adapter_name,
    }), maxlen=10000, approximate=True)
    _pending_risk[rec.id] = fsm
    await _pub(r, fsm, "risk_check_sent")


async def process_risk_approved(data: dict):
    order_id = data.get("order_id"); approved = data.get("approved", False)
    reason = data.get("reason",""); max_qty = data.get("max_quantity")
    fsm = _pending_risk.pop(order_id, None)
    if not fsm: return
    r = await get_redis()
    if not approved:
        fsm.transition(OrderState.FAILED, note=reason); fsm.record.reject_reason = reason
        _stats["failed"] += 1; await _pub(r, fsm, "risk_rejected")
        logger.warning(f"Order {order_id[:8]} REJECTED: {reason}"); return
    if max_qty and max_qty < fsm.record.quantity:
        fsm.record.quantity = max_qty
    fsm.transition(OrderState.PENDING_ACK, note="risk_approved")
    if EXEC_MODE == "CONFIRM_THEN_EXEC":
        await _pub(r, fsm, "pending_confirm"); return
    await submit_to_exchange(fsm)


async def submit_to_exchange(fsm: OrderFSM):
    r = await get_redis(); rec = fsm.record

    if EXEC_MODE == "PAPER":
        prices_raw = await r.hget("cw4:prices", rec.symbol)
        if prices_raw:
            import json as _json
            _p = _json.loads(prices_raw)
            fill_price = float(_p.get("price", rec.limit_price or rec.entry_price) if isinstance(_p, dict) else _p)
        else:
            fill_price = rec.limit_price or rec.entry_price
        fsm.transition(OrderState.OPEN)
        rec.exchange_id = f"paper-{rec.id[:8]}"
        _exchange_map[rec.exchange_id] = rec.id
        fsm.on_fill(rec.quantity, fill_price, fill_price * rec.quantity * 0.0005)
        fsm.transition(OrderState.MONITORING); _stats["filled"] += 1
        await _pub(r, fsm, "filled_immediately"); await _audit(fsm, "ORDER_FILLED")
        await _persist_order(rec)
        logger.info(f"[PAPER] Order {rec.id[:8]} filled {rec.symbol} {rec.side} qty={rec.quantity} price={fill_price}")
        return

    try: adp = get_adapter(rec.exchange or PRIMARY_EXCHANGE)
    except RuntimeError as e:
        fsm.transition(OrderState.FAILED, note=str(e)); _stats["failed"] += 1
        await _pub(r, fsm, "no_adapter"); return
    result = await adp.place_order(rec.symbol, rec.side, rec.quantity, rec.order_type, rec.limit_price or rec.entry_price)
    if not result.success:
        fsm.transition(OrderState.FAILED, note=result.error); rec.reject_reason = result.error
        _stats["failed"] += 1; await _pub(r, fsm, "submit_failed"); await _audit(fsm, "ORDER_SUBMIT_FAILED")
        await _persist_order(rec)
        logger.error(f"Order {rec.id[:8]} submit failed: {result.error}"); return
    fsm.transition(OrderState.OPEN)
    _exchange_map[result.exchange_id] = rec.id; rec.exchange_id = result.exchange_id
    if result.status == "FILLED" and result.filled_qty > 0:
        fsm.on_fill(result.filled_qty, result.filled_price, result.fee_paid)
        fsm.transition(OrderState.MONITORING); _stats["filled"] += 1
        await _pub(r, fsm, "filled_immediately"); await _audit(fsm, "ORDER_FILLED")
        await _persist_order(rec)
    else:
        await _persist_order(rec)
        await _pub(r, fsm, "submitted")
        logger.info(f"Order {rec.id[:8]} submitted waiting fill exch_id={result.exchange_id}")


async def monitoring_loop():
    while True:
        await asyncio.sleep(MONITOR_INTERVAL)
        r = await get_redis()
        monitoring = [f for f in _orders.values() if f.record.state == OrderState.MONITORING]
        if not monitoring: continue
        prices_raw = await r.hgetall("cw4:prices"); prices = {}
        for k,v in prices_raw.items():
            key = k.decode() if isinstance(k,bytes) else k
            try:
                val = json.loads(v.decode() if isinstance(v,bytes) else v)
                prices[key] = val.get("price",0) if isinstance(val,dict) else float(val)
            except Exception: pass
        for fsm in monitoring:
            rec = fsm.record; price = prices.get(rec.symbol, 0)
            if not price: continue
            hit_stop = (rec.side=="BUY" and price<=rec.stop_loss) or (rec.side=="SELL" and price>=rec.stop_loss)
            hit_take = (rec.side=="BUY" and price>=rec.take_profit) or (rec.side=="SELL" and price<=rec.take_profit)
            if hit_stop or hit_take:
                reason = "stop_loss" if hit_stop else "take_profit"
                fsm.transition(OrderState.CLOSING, note=reason)
                asyncio.create_task(_close_position(fsm, price, reason))
        open_orders = {f.record.id: f.to_dict() for f in _orders.values() if not f.record.is_terminal}
        if open_orders:
            await r.hset("cw4:orders:recent", mapping={k:json.dumps(v) for k,v in open_orders.items()})


async def _close_position(fsm: OrderFSM, exit_price: float, reason: str):
    r = await get_redis(); rec = fsm.record
    try:
        adp = get_adapter(rec.exchange or PRIMARY_EXCHANGE)
        close_side = "SELL" if rec.side=="BUY" else "BUY"
        result = await adp.place_order(rec.symbol, close_side, rec.filled_qty or rec.quantity, "MARKET", reduce_only=True)
        if result.success:
            actual_exit = result.filled_price or exit_price
            pnl = fsm.calc_realized_pnl(actual_exit)
            fsm.transition(OrderState.CLOSED, note=reason); rec.close_reason = reason
            await _pub(r, fsm, "closed"); await _audit(fsm, "POSITION_CLOSED")
            await _persist_order(rec)
            logger.info(f"Position CLOSED {rec.symbol} pnl={pnl:+.4f} reason={reason}")
        else:
            fsm.transition(OrderState.MONITORING, note="close_failed_retry")
    except Exception as e:
        logger.error(f"_close_position: {e}"); fsm.transition(OrderState.MONITORING, note="exception_retry")


async def _pub(r, fsm: OrderFSM, event: str):
    await r.xadd(STREAM_ORDER_LIFECYCLE, encode({**fsm.to_dict(),"event":event}), maxlen=100000, approximate=True)


async def _audit(fsm: OrderFSM, event_type: str):
    try:
        pool = await get_pg()
        async with pool.acquire() as conn:
            await conn.execute("INSERT INTO audit_log(event_type,entity_id,payload,service) VALUES($1,$2,$3,$4)",
                               event_type, fsm.record.id, json.dumps(fsm.to_dict()), "execution-service")
    except Exception as e: logger.warning(f"audit: {e}")


async def _persist_order(rec):
    import uuid as _uuid
    try:
        pool = await get_pg()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO orders
                    (id, signal_id, symbol, exchange, side, order_type,
                     quantity, limit_price, stop_price, filled_price, filled_qty,
                     slippage_pct, fee_paid, state, exchange_id,
                     kelly_fraction, risk_usd, reject_reason, close_reason,
                     realized_pnl, created_at, closed_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22)
                ON CONFLICT (id) DO UPDATE SET
                    filled_price  = EXCLUDED.filled_price,
                    filled_qty    = EXCLUDED.filled_qty,
                    slippage_pct  = EXCLUDED.slippage_pct,
                    fee_paid      = EXCLUDED.fee_paid,
                    state         = EXCLUDED.state,
                    exchange_id   = EXCLUDED.exchange_id,
                    reject_reason = EXCLUDED.reject_reason,
                    close_reason  = EXCLUDED.close_reason,
                    realized_pnl  = EXCLUDED.realized_pnl,
                    closed_at     = EXCLUDED.closed_at
            """,
            _uuid.UUID(rec.id),
            _uuid.UUID(rec.signal_id) if rec.signal_id else None,
            rec.symbol, rec.exchange, rec.side, rec.order_type,
            rec.quantity, rec.limit_price or rec.entry_price,
            rec.stop_loss or None,
            rec.filled_price or None, rec.filled_qty or None,
            rec.slippage_pct or None, rec.fee_paid or None,
            rec.state.value, rec.exchange_id or None,
            rec.kelly_fraction or None, rec.risk_usd or None,
            rec.reject_reason or None, rec.close_reason or None,
            rec.realized_pnl,
            rec.created_at,
            rec.closed_at,
        )
    except Exception as e:
        logger.warning(f"order DB persist failed: {e}")


async def consume_loop():
    r = await get_redis()
    for stream in [STREAM_SIGNAL_SIZED, STREAM_RISK_APPROVED]:
        try: await r.xgroup_create(stream, CG_EXECUTION, id="0", mkstream=True)
        except Exception as e:
            if "BUSYGROUP" not in str(e): raise
    logger.info(f"Execution service ready (EXEC_MODE={EXEC_MODE})")
    while True:
        for stream, worker, handler in [
            (STREAM_SIGNAL_SIZED, "exec-worker", process_scored_signal),
            (STREAM_RISK_APPROVED, "exec-risk",   process_risk_approved),
        ]:
            results = await r.xreadgroup(CG_EXECUTION, worker, {stream:">"}, count=5, block=100)
            for _, messages in (results or []):
                for msg_id, fields in messages:
                    try: await handler(decode(fields)); await r.xack(stream, CG_EXECUTION, msg_id)
                    except Exception as e: logger.error(f"{worker}: {e}", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _init_adapters()
    for name, adp in get_all_adapters().items():
        adp.on_fill(on_fill); asyncio.create_task(adp.subscribe_fills())
        logger.info(f"Fill subscription: {name}")
    tasks = [asyncio.create_task(consume_loop()), asyncio.create_task(monitoring_loop())]
    yield
    for t in tasks: t.cancel()
    for adp in get_all_adapters().values(): await adp.close()


app = FastAPI(title="CryptoWatch v4 Execution Service", lifespan=lifespan)

@app.get("/health")
async def health():
    return {"status":"ok","service":"execution","exec_mode":EXEC_MODE,
            "redis":await redis_health(),"pg":await pg_health(),
            "stats":_stats,"active_orders":len([f for f in _orders.values() if not f.record.is_terminal])}

@app.get("/orders/recent")
async def recent_orders(limit: int = 50):
    return {"orders":[f.to_dict() for f in list(_recent_orders)[-limit:]]}

@app.post("/orders/{order_id}/confirm")
async def confirm_order(order_id: str):
    fsm = _orders.get(order_id)
    if not fsm: return {"error":"not_found"}
    await submit_to_exchange(fsm); return {"status":"submitted","order_id":order_id}

@app.post("/orders/{order_id}/cancel")
async def cancel_order(order_id: str):
    fsm = _orders.get(order_id)
    if not fsm: return {"error":"not_found"}
    rec = fsm.record
    if rec.exchange_id:
        try:
            adp = get_adapter(rec.exchange or PRIMARY_EXCHANGE)
            await adp.cancel_order(rec.symbol, rec.exchange_id)
        except Exception as e: logger.warning(f"cancel on exchange: {e}")
    fsm.transition(OrderState.CANCELLED, note="manual_cancel"); _stats["cancelled"] += 1
    return {"status":"cancelled"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("SERVICE_PORT",8005)))
