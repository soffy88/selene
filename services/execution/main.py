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
    STREAM_SIGNAL_SIZED,
    STREAM_RISK_CHECK,
    STREAM_RISK_APPROVED,
    STREAM_ORDER_LIFECYCLE,
    STREAM_SYSTEM_ALERTS,
    encode,
    decode,
    run_forever,
)
from shared.models.signal import ScoredSignal, Direction
from services.execution.statemachine.order_fsm import OrderFSM, OrderRecord, OrderState
from services.execution.slippage.model import SlippageModel
from services.execution.adapters.base import (
    get_adapter,
    register_adapter,
    FillEvent,
    get_all_adapters,
    OrderResult,
)
from services.execution.routing.smart_router import SmartRouter

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

EXEC_MODE = os.getenv("EXEC_MODE", "NOTIFY_ONLY")
PRIMARY_EXCHANGE = os.getenv("PRIMARY_EXCHANGE", "binance")
MONITOR_INTERVAL = float(os.getenv("MONITOR_INTERVAL_S", "5"))
MAX_SIGNAL_AGE_S = float(os.getenv("MAX_SIGNAL_AGE_S", "30"))  # quote-staleness guard
RECONCILE_INTERVAL_S = float(
    os.getenv("RECONCILE_INTERVAL_S", "60")
)  # exchange<->state reconcile
ORDER_TTL_S = float(
    os.getenv("ORDER_TTL_S", "300")
)  # cancel a working LIMIT that hasn't filled in N s
CG_EXECUTION = "execution-service"

_last_reconcile = {"ts": 0.0, "divergences": 0}
_fill_lock = asyncio.Lock()  # serializes fill application across WS + reconcile paths

_slippage_model = SlippageModel()
_router = SmartRouter()
_orders: dict[str, OrderFSM] = {}
_exchange_map: dict[str, str] = {}
_stop_orders: dict[
    str, str
] = {}  # order_id → exchange-native stop id (best-effort bracket)
_pending_risk: dict[str, OrderFSM] = {}
_recent_orders: deque = deque(maxlen=500)
_stats = {
    "queued": 0,
    "filled": 0,
    "failed": 0,
    "cancelled": 0,
    "risk_rejected": 0,
    "stale_dropped": 0,
}


def _signal_age_s(signal) -> "float | None":
    """Age of a ScoredSignal in seconds (None if it carries no usable timestamp)."""
    ts = getattr(signal, "timestamp", None)
    if ts is None:
        return None
    try:
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (now - ts).total_seconds()
    except Exception:
        return None


def _client_oid(order_id: str, kind: str = "O") -> str:
    """Deterministic exchange idempotency key from the internal order id.
    `kind` distinguishes the opening order ('O') from its reduce-only close ('C') so the two
    never collide. Reused verbatim on any retry → the exchange dedupes duplicate placements."""
    h = order_id.replace("-", "")[:31]
    return f"{h}{kind}"


def _init_adapters():
    from services.execution.adapters.binance import BinanceAdapter
    from services.execution.adapters.okx import OKXAdapter

    testnet = os.getenv("ENVIRONMENT", "development") != "production"
    bkey = os.getenv("BINANCE_API_KEY", "")
    bsec = os.getenv("BINANCE_API_SECRET", "")
    okey = os.getenv("OKX_API_KEY", "")
    osec = os.getenv("OKX_API_SECRET", "")
    opass = os.getenv("OKX_PASSPHRASE", "")
    if bkey:
        register_adapter("binance", BinanceAdapter(bkey, bsec, testnet))
        logger.info(f"Binance {'TESTNET' if testnet else 'LIVE'}")
    if okey:
        register_adapter("okx", OKXAdapter(okey, osec, opass, testnet))
        logger.info(f"OKX {'TESTNET' if testnet else 'LIVE'}")
    if not bkey and not okey:
        logger.warning("No API keys — NOTIFY_ONLY forced")


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
            if regime == "HIGH_VOLATILITY":
                return 1.5
            if regime in ("TRENDING_UP", "TRENDING_DOWN"):
                return 0.80
            if regime == "RANGING":
                return 0.50
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


async def _apply_fill_locked(fsm, qty: float, price: float, fee: float, is_final: bool):
    """Apply a fill to the FSM and publish. Caller MUST hold _fill_lock so that the
    WebSocket fill path and the reconcile path never interleave and double-count."""
    r = await get_redis()
    fsm.on_fill(qty, price, fee)
    if is_final or fsm.record.state == OrderState.FILLED:
        if fsm.record.state == OrderState.FILLED:
            fsm.transition(OrderState.MONITORING)
            _stats["filled"] += 1
            await _pub(r, fsm, "filled")
            await _audit(fsm, "ORDER_FILLED")
            logger.info(
                f"✅ FILLED {fsm.record.symbol} avg={fsm.record.filled_price:.4f} slip={fsm.record.slippage_pct:.4f}%"
            )
    else:
        await _pub(r, fsm, "partial_fill")


async def on_fill(event: FillEvent):
    order_id = _exchange_map.get(event.exchange_id)
    if not order_id:
        return
    fsm = _orders.get(order_id)
    if not fsm:
        return
    async with _fill_lock:
        await _apply_fill_locked(
            fsm, event.filled_qty, event.filled_price, event.fee, event.is_final
        )


async def process_scored_signal(data: dict):
    if EXEC_MODE == "NOTIFY_ONLY":
        logger.info(
            f"[NOTIFY_ONLY] {data.get('signal_type')} {data.get('symbol')} p={float(data.get('win_probability', 0)):.0%}"
        )
        return
    try:
        signal = ScoredSignal.from_dict(data)
    except Exception as e:
        logger.error(f"parse error: {e}")
        return
    if not signal.is_actionable or signal.direction == Direction.NEUTRAL:
        return
    # ── 报价/信号时效闸门 ── reject signals stale enough that the quote we'd trade
    # against has likely moved; crypto marks go stale in seconds.
    age = _signal_age_s(signal)
    if age is not None and age > MAX_SIGNAL_AGE_S:
        logger.warning(
            f"DROP stale signal {signal.symbol} age={age:.1f}s > {MAX_SIGNAL_AGE_S}s"
        )
        _stats["stale_dropped"] += 1
        return
    # 必须已通过 portfolio sizing（position_size + allocated_capital）
    position_size = float(data.get("position_size", 0) or 0)
    allocated = float(data.get("allocated_capital", 0) or 0)
    if position_size <= 0 or allocated <= 0:
        logger.warning(
            f"DROP unsized signal {signal.symbol} position_size={position_size} allocated={allocated}"
        )
        return
    side = "BUY" if signal.direction == Direction.LONG else "SELL"
    r = await get_redis()
    # ── 执行级 HALT（对账发现幽灵敞口时熔断）──
    if await r.get("cw4:execution:halt"):
        logger.warning(f"EXECUTION HALT active — dropping signal {signal.id[:8]}")
        _stats["risk_rejected"] += 1
        return
    # ── 强制风控 Gate ──
    risk_raw = await r.get("cw4:risk:status")
    if risk_raw:
        rs = json.loads(risk_raw)
        if not rs.get("new_trades_allowed", True):
            logger.warning(
                f"RISK GATE BLOCKED level={rs.get('drawdown_level')} signal={signal.id[:8]}"
            )
            _stats["risk_rejected"] += 1
            return
    # ── 路由（使用 portfolio sized 的真实 allocated_capital）──
    try:
        plan = await _router.route(signal.symbol, side, allocated)
        adapter_name = plan.splits[0]["adapter"] if plan.splits else PRIMARY_EXCHANGE
    except Exception as e:
        logger.warning(f"Router failed: {e}")
        adapter_name = PRIMARY_EXCHANGE
    # ── 实时盘口滑点（真实 spread + symbol realized vol + 估算 ADV）──
    try:
        adp = get_adapter(adapter_name)
        book = await adp.get_orderbook(signal.symbol)
        bids = book.get("bids", [[0, 0]])
        asks = book.get("asks", [[0, 0]])
        spread = (asks[0][0] - bids[0][0]) / asks[0][0] if asks and bids else 0.002
        # 估算 symbol 近期实现年化波动率
        realized_vol = await _estimate_realized_vol(r, signal.symbol)
        # 估算 ADV（默认: price × 5_000_000 units/day；后续可接 binance 24hTicker）
        adv_usd = await _estimate_daily_volume_usd(r, signal.symbol, signal.entry_price)
        slip = _slippage_model.estimate(
            allocated, realized_vol, adv_usd, spread * 100, "LIMIT"
        )
    except Exception as e:
        logger.warning(f"slippage estimate failed: {e}")
        slip = None
    rec = OrderRecord(
        signal_id=signal.id,
        symbol=signal.symbol,
        side=side,
        order_type="LIMIT",
        quantity=position_size,
        entry_price=signal.entry_price,
        stop_loss=signal.stop_loss,
        take_profit=signal.take_profit,
        limit_price=signal.entry_price,
        exchange=adapter_name,
        kelly_fraction=float(data.get("kelly_fraction", 0)),
        risk_usd=float(data.get("risk_usd", 0)),
    )
    fsm = OrderFSM(rec)
    _orders[rec.id] = fsm
    _recent_orders.append(fsm)
    _stats["queued"] += 1
    await _persist_order(rec)
    fsm.transition(OrderState.SLIPPAGE_ESTIMATE)
    if slip and slip.total > 1.0:
        fsm.transition(OrderState.CANCELLED, note=f"slippage {slip.total:.2f}%")
        _stats["cancelled"] += 1
        return
    fsm.transition(OrderState.ROUTING)
    fsm.transition(OrderState.SUBMITTING)
    await r.xadd(
        STREAM_RISK_CHECK,
        encode(
            {
                "order_id": rec.id,
                "signal_id": signal.id,
                "symbol": signal.symbol,
                "side": side,
                "quantity": rec.quantity,
                "entry_price": signal.entry_price,
                "stop_price": signal.stop_loss,
                "allocated_usd": allocated,
                "win_probability": signal.win_probability,
                "regime": signal.regime.value,
                "slippage_pct": slip.total if slip else 0,
                "adapter": adapter_name,
            }
        ),
        maxlen=10000,
        approximate=True,
    )
    _pending_risk[rec.id] = fsm
    await _pub(r, fsm, "risk_check_sent")


async def process_risk_approved(data: dict):
    order_id = data.get("order_id")
    approved = data.get("approved", False)
    reason = data.get("reason", "")
    max_qty = data.get("max_quantity")
    fsm = _pending_risk.pop(order_id, None)
    if not fsm:
        return
    r = await get_redis()
    if not approved:
        fsm.transition(OrderState.FAILED, note=reason)
        fsm.record.reject_reason = reason
        _stats["failed"] += 1
        await _pub(r, fsm, "risk_rejected")
        logger.warning(f"Order {order_id[:8]} REJECTED: {reason}")
        return
    if max_qty and max_qty < fsm.record.quantity:
        fsm.record.quantity = max_qty
    fsm.transition(OrderState.PENDING_ACK, note="risk_approved")
    if EXEC_MODE == "CONFIRM_THEN_EXEC":
        await _pub(r, fsm, "pending_confirm")
        return
    await submit_to_exchange(fsm)


def ttl_action(state, filled_qty: float, age_s: float, ttl_s: float = ORDER_TTL_S):
    """Decide what to do with a working order at reconcile time (item #11).
    Returns None (leave it), "accept_partial", or "cancel". Pure for testing."""
    if state not in (OrderState.OPEN, OrderState.PARTIALLY_FILLED):
        return None
    if age_s <= ttl_s:
        return None
    return "accept_partial" if filled_qty > 0 else "cancel"


async def submit_to_exchange(fsm: OrderFSM):
    r = await get_redis()
    rec = fsm.record

    if EXEC_MODE == "PAPER":
        prices_raw = await r.hget("cw4:prices", rec.symbol)
        if prices_raw:
            import json as _json

            _p = _json.loads(prices_raw)
            fill_price = float(
                _p.get("price", rec.limit_price or rec.entry_price)
                if isinstance(_p, dict)
                else _p
            )
        else:
            fill_price = rec.limit_price or rec.entry_price
        fsm.transition(OrderState.OPEN)
        rec.exchange_id = f"paper-{rec.id[:8]}"
        _exchange_map[rec.exchange_id] = rec.id
        fsm.on_fill(rec.quantity, fill_price, fill_price * rec.quantity * 0.0005)
        fsm.transition(OrderState.MONITORING)
        _stats["filled"] += 1
        await _pub(r, fsm, "filled_immediately")
        await _audit(fsm, "ORDER_FILLED")
        await _persist_order(rec)
        logger.info(
            f"[PAPER] Order {rec.id[:8]} filled {rec.symbol} {rec.side} qty={rec.quantity} price={fill_price}"
        )
        return

    try:
        adp = get_adapter(rec.exchange or PRIMARY_EXCHANGE)
    except RuntimeError as e:
        fsm.transition(OrderState.FAILED, note=str(e))
        _stats["failed"] += 1
        await _pub(r, fsm, "no_adapter")
        return
    result = await adp.place_order(
        rec.symbol,
        rec.side,
        rec.quantity,
        rec.order_type,
        rec.limit_price or rec.entry_price,
        client_order_id=_client_oid(rec.id, "O"),
    )
    if not result.success:
        fsm.transition(OrderState.FAILED, note=result.error)
        rec.reject_reason = result.error
        _stats["failed"] += 1
        await _pub(r, fsm, "submit_failed")
        await _audit(fsm, "ORDER_SUBMIT_FAILED")
        await _persist_order(rec)
        logger.error(f"Order {rec.id[:8]} submit failed: {result.error}")
        return
    fsm.transition(OrderState.OPEN)
    _exchange_map[result.exchange_id] = rec.id
    rec.exchange_id = result.exchange_id
    if result.status == "FILLED" and result.filled_qty > 0:
        fsm.on_fill(result.filled_qty, result.filled_price, result.fee_paid)
        fsm.transition(OrderState.MONITORING)
        _stats["filled"] += 1
        await _place_protective_stop(
            adp, rec
        )  # native exchange stop (survives outages/gaps)
        await _pub(r, fsm, "filled_immediately")
        await _audit(fsm, "ORDER_FILLED")
        await _persist_order(rec)
    else:
        await _persist_order(rec)
        await _pub(r, fsm, "submitted")
        logger.info(
            f"Order {rec.id[:8]} submitted waiting fill exch_id={result.exchange_id}"
        )


async def monitoring_loop():
    while True:
        await asyncio.sleep(MONITOR_INTERVAL)
        r = await get_redis()
        monitoring = [
            f for f in _orders.values() if f.record.state == OrderState.MONITORING
        ]
        if not monitoring:
            continue
        prices_raw = await r.hgetall("cw4:prices")
        prices = {}
        for k, v in prices_raw.items():
            key = k.decode() if isinstance(k, bytes) else k
            try:
                val = json.loads(v.decode() if isinstance(v, bytes) else v)
                prices[key] = (
                    val.get("price", 0) if isinstance(val, dict) else float(val)
                )
            except Exception:
                pass
        for fsm in monitoring:
            rec = fsm.record
            price = prices.get(rec.symbol, 0)
            if not price:
                continue
            hit_stop = (rec.side == "BUY" and price <= rec.stop_loss) or (
                rec.side == "SELL" and price >= rec.stop_loss
            )
            hit_take = rec.take_profit > 0 and (
                (rec.side == "BUY" and price >= rec.take_profit)
                or (rec.side == "SELL" and price <= rec.take_profit)
            )
            if hit_stop or hit_take:
                reason = "stop_loss" if hit_stop else "take_profit"
                fsm.transition(OrderState.CLOSING, note=reason)
                asyncio.create_task(_close_position(fsm, price, reason))
        open_orders = {
            f.record.id: f.to_dict()
            for f in _orders.values()
            if not f.record.is_terminal
        }
        if open_orders:
            await r.hset(
                "cw4:orders:recent",
                mapping={k: json.dumps(v) for k, v in open_orders.items()},
            )


async def reconcile_loop():
    """Periodically reconcile internal state against the exchange (Phase 7b):
    - recover fills the WebSocket may have missed (poll get_order on open orders),
    - detect exchange positions with no tracked order (phantom exposure),
    - write an execution heartbeat for an external dead-man's switch,
    - trip cw4:execution:halt on serious divergence so new orders are blocked.
    """
    while True:
        await asyncio.sleep(RECONCILE_INTERVAL_S)
        try:
            r = await get_redis()
            now = time.time()
            open_n = len([f for f in _orders.values() if not f.record.is_terminal])
            # dead-man heartbeat (external watcher alerts if this goes stale)
            await r.set(
                "cw4:execution:heartbeat",
                json.dumps({"ts": now, "open_orders": open_n}),
            )

            if EXEC_MODE == "PAPER":
                continue  # no live exchange to reconcile against

            # 1) recover missed fills on live, non-terminal orders
            for fsm in list(_orders.values()):
                rec = fsm.record
                if (
                    rec.is_terminal
                    or not rec.exchange_id
                    or rec.exchange_id.startswith("paper-")
                ):
                    continue
                try:
                    adp = get_adapter(rec.exchange or PRIMARY_EXCHANGE)
                    o = await adp.get_order(rec.symbol, rec.exchange_id)
                except Exception:
                    continue
                if not o.success:
                    continue
                # Re-read the recorded fill and apply the delta under _fill_lock so a WS fill
                # arriving between the get_order() above and here cannot be double-counted.
                async with _fill_lock:
                    recorded = rec.filled_qty or 0.0
                    if o.filled_qty > recorded + 1e-9:
                        logger.warning(
                            f"RECONCILE missed fill {rec.id[:8]}: exch={o.filled_qty} recorded={recorded}"
                        )
                        await _apply_fill_locked(
                            fsm,
                            o.filled_qty - recorded,
                            o.filled_price,
                            0.0,
                            str(o.status).upper() in ("FILLED", "EFFECTIVE"),
                        )
                        await _audit(fsm, "RECONCILE_MISSED_FILL")

            # 1b) working-order TTL (item #11): cancel a live LIMIT that hasn't filled
            # within ORDER_TTL_S. A partial fill is accepted (→ MONITORING); a wholly
            # unfilled order is cancelled (→ CANCELLED).
            for fsm in list(_orders.values()):
                rec = fsm.record
                if not rec.exchange_id or rec.exchange_id.startswith("paper-"):
                    continue
                age = (datetime.utcnow() - rec.created_at).total_seconds()
                action = ttl_action(rec.state, rec.filled_qty or 0.0, age, ORDER_TTL_S)
                if action is None:
                    continue
                try:
                    adp = get_adapter(rec.exchange or PRIMARY_EXCHANGE)
                    cancel = await adp.cancel_order(rec.symbol, rec.exchange_id)
                except Exception as e:
                    logger.warning(f"order TTL cancel error {rec.id[:8]}: {e}")
                    continue
                if not cancel.success:
                    logger.warning(
                        f"order TTL cancel rejected {rec.id[:8]}: {cancel.error}"
                    )
                    continue
                if rec.filled_qty > 0:
                    fsm.transition(
                        OrderState.MONITORING, note=f"ttl_partial_accept age={age:.0f}s"
                    )
                    _stats["filled"] += 1
                    await _pub(r, fsm, "ttl_partial_accepted")
                else:
                    fsm.transition(
                        OrderState.CANCELLED, note=f"ttl_unfilled age={age:.0f}s"
                    )
                    _stats["cancelled"] += 1
                    await _pub(r, fsm, "ttl_cancelled")
                await _audit(fsm, "ORDER_TTL_CANCEL")
                await _persist_order(rec)
                logger.info(
                    f"order {rec.id[:8]} TTL-{'partial' if rec.filled_qty > 0 else 'cancel'} after {age:.0f}s"
                )

            # 2) phantom-exposure detection: exchange position with no tracked order
            divergences = 0
            tracked = {
                f.record.symbol for f in _orders.values() if not f.record.is_terminal
            }
            for name, adp in get_all_adapters().items():
                try:
                    positions = await adp.get_positions()
                except Exception:
                    continue
                for pos in positions:
                    if pos.size > 0 and pos.symbol not in tracked:
                        divergences += 1
                        logger.critical(
                            f"RECONCILE divergence: {name} holds {pos.symbol} {pos.side} "
                            f"size={pos.size} with no tracked order"
                        )

            _last_reconcile.update(ts=now, divergences=divergences)
            await r.set("cw4:execution:reconcile", json.dumps(_last_reconcile))
            if divergences > 0:
                await r.set(
                    "cw4:execution:halt",
                    json.dumps(
                        {
                            "reason": "position_divergence",
                            "ts": now,
                            "count": divergences,
                        }
                    ),
                )
                logger.critical(
                    f"EXECUTION HALT set: {divergences} unreconciled exchange position(s); "
                    "clear with POST /execution/halt/clear after manual review"
                )
        except Exception as e:
            logger.error(f"reconcile_loop error: {e}")


async def _close_position(fsm: OrderFSM, exit_price: float, reason: str):
    r = await get_redis()
    rec = fsm.record

    if EXEC_MODE == "PAPER":
        pnl = fsm.calc_realized_pnl(exit_price)
        fsm.transition(OrderState.CLOSED, note=reason)
        rec.close_reason = reason
        await _pub(r, fsm, "closed")
        await _audit(fsm, "POSITION_CLOSED")
        await _persist_order(rec)
        logger.info(
            f"[PAPER] Position CLOSED {rec.symbol} pnl={pnl:+.4f} reason={reason} exit={exit_price}"
        )
        return

    try:
        adp = get_adapter(rec.exchange or PRIMARY_EXCHANGE)
        close_side = "SELL" if rec.side == "BUY" else "BUY"
        result = await adp.place_order(
            rec.symbol,
            close_side,
            rec.filled_qty or rec.quantity,
            "MARKET",
            reduce_only=True,
            client_order_id=_client_oid(rec.id, "C"),
        )
        if result.success:
            await _cancel_protective_stop(adp, rec)  # drop the now-orphaned native stop
            actual_exit = result.filled_price or exit_price
            pnl = fsm.calc_realized_pnl(actual_exit)
            fsm.transition(OrderState.CLOSED, note=reason)
            rec.close_reason = reason
            await _pub(r, fsm, "closed")
            await _audit(fsm, "POSITION_CLOSED")
            await _persist_order(rec)
            logger.info(f"Position CLOSED {rec.symbol} pnl={pnl:+.4f} reason={reason}")
        else:
            fsm.transition(OrderState.MONITORING, note="close_failed_retry")
    except Exception as e:
        logger.error(f"_close_position: {e}")
        fsm.transition(OrderState.MONITORING, note="exception_retry")


async def _place_protective_stop(adp, rec) -> bool:
    """Place an exchange-native protective stop after a live fill (best-effort bracket).

    The in-process monitoring_loop is only a backstop: it depends on this service being
    alive and the price feed being fresh, so it cannot protect against a service outage or
    a gap. A native stop lives on the exchange and fires regardless. We place it reduce-only
    on the opposite side at rec.stop_loss. On failure we keep the position (the in-process
    monitor still guards it) but emit a loud alert — a live position without its native stop
    is a risk event, not a silent condition.

    Returns True if a native stop was placed. Never raises (no-throw execution contract)."""
    if not rec.stop_loss or rec.stop_loss <= 0:
        return False
    stop_side = "SELL" if rec.side == "BUY" else "BUY"
    try:
        res = await adp.place_stop_order(
            rec.symbol,
            stop_side,
            rec.filled_qty or rec.quantity,
            rec.stop_loss,
            reduce_only=True,
            client_order_id=_client_oid(rec.id, "S"),
        )
    except Exception as e:  # defensive: adapter should never throw, but guard anyway
        res = OrderResult(success=False, error=str(e))
    if res.success:
        _stop_orders[rec.id] = res.exchange_id
        logger.info(
            f"Protective stop placed {rec.symbol} {stop_side} @ {rec.stop_loss} id={res.exchange_id}"
        )
        return True
    # No native stop — alert so the gap is visible; in-process monitor remains the backstop.
    logger.error(
        f"PROTECTIVE STOP FAILED {rec.symbol} order={rec.id[:8]}: {res.error} "
        f"— relying on in-process monitor only"
    )
    try:
        r = await get_redis()
        await r.xadd(
            STREAM_SYSTEM_ALERTS,
            encode(
                {
                    "type": "risk_alert",
                    "severity": "high",
                    "source": "execution",
                    "msg": f"native protective stop failed for {rec.symbol} ({res.error}); "
                    f"position guarded by in-process monitor only",
                    "order_id": rec.id,
                }
            ),
            maxlen=100000,
            approximate=True,
        )
    except Exception as e:
        logger.warning(f"stop-fail alert publish: {e}")
    return False


async def _cancel_protective_stop(adp, rec):
    """Cancel a position's native stop once it's closed (avoid an orphaned reduce-only stop)."""
    stop_id = _stop_orders.pop(rec.id, None)
    if not stop_id:
        return
    try:
        await adp.cancel_order(rec.symbol, stop_id)
    except Exception as e:
        logger.warning(f"cancel protective stop {stop_id}: {e}")


async def _pub(r, fsm: OrderFSM, event: str):
    await r.xadd(
        STREAM_ORDER_LIFECYCLE,
        encode({**fsm.to_dict(), "event": event}),
        maxlen=100000,
        approximate=True,
    )


async def _audit(fsm: OrderFSM, event_type: str):
    try:
        pool = await get_pg()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO audit_log(event_type,entity_id,payload,service) VALUES($1,$2,$3,$4)",
                event_type,
                fsm.record.id,
                json.dumps(fsm.to_dict()),
                "execution-service",
            )
    except Exception as e:
        logger.warning(f"audit: {e}")


async def _persist_order(rec):
    import uuid as _uuid

    try:
        pool = await get_pg()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO orders
                    (id, signal_id, symbol, exchange, side, order_type,
                     quantity, limit_price, stop_price, take_profit, filled_price, filled_qty,
                     slippage_pct, fee_paid, state, exchange_id,
                     kelly_fraction, risk_usd, reject_reason, close_reason,
                     realized_pnl, created_at, closed_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22,$23)
                ON CONFLICT (id) DO UPDATE SET
                    filled_price  = EXCLUDED.filled_price,
                    filled_qty    = EXCLUDED.filled_qty,
                    slippage_pct  = EXCLUDED.slippage_pct,
                    fee_paid      = EXCLUDED.fee_paid,
                    state         = EXCLUDED.state,
                    exchange_id   = EXCLUDED.exchange_id,
                    take_profit   = EXCLUDED.take_profit,
                    reject_reason = EXCLUDED.reject_reason,
                    close_reason  = EXCLUDED.close_reason,
                    realized_pnl  = EXCLUDED.realized_pnl,
                    closed_at     = EXCLUDED.closed_at
            """,
                _uuid.UUID(rec.id),
                _uuid.UUID(rec.signal_id) if rec.signal_id else None,
                rec.symbol,
                rec.exchange,
                rec.side,
                rec.order_type,
                rec.quantity,
                rec.limit_price or rec.entry_price,
                rec.stop_loss or None,
                rec.take_profit or None,
                rec.filled_price or None,
                rec.filled_qty or None,
                rec.slippage_pct or None,
                rec.fee_paid or None,
                rec.state.value,
                rec.exchange_id or None,
                rec.kelly_fraction or None,
                rec.risk_usd or None,
                rec.reject_reason or None,
                rec.close_reason or None,
                rec.realized_pnl,
                rec.created_at,
                rec.closed_at,
            )
    except Exception as e:
        logger.warning(f"order DB persist failed: {e}")


async def consume_loop():
    r = await get_redis()
    for stream in [STREAM_SIGNAL_SIZED, STREAM_RISK_APPROVED]:
        try:
            await r.xgroup_create(stream, CG_EXECUTION, id="0", mkstream=True)
        except Exception as e:
            if "BUSYGROUP" not in str(e):
                raise
    logger.info(f"Execution service ready (EXEC_MODE={EXEC_MODE})")
    while True:
        for stream, worker, handler in [
            (STREAM_SIGNAL_SIZED, "exec-worker", process_scored_signal),
            (STREAM_RISK_APPROVED, "exec-risk", process_risk_approved),
        ]:
            results = await r.xreadgroup(
                CG_EXECUTION, worker, {stream: ">"}, count=5, block=100
            )
            for _, messages in results or []:
                for msg_id, fields in messages:
                    try:
                        await handler(decode(fields))
                        await r.xack(stream, CG_EXECUTION, msg_id)
                    except Exception as e:
                        logger.error(f"{worker}: {e}", exc_info=True)


async def _recover_monitoring_orders():
    """On startup: reload MONITORING orders from DB into _orders so the monitoring_loop picks them up."""
    import uuid as _uuid

    try:
        pool = await get_pg()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, signal_id, symbol, exchange, side, order_type, "
                "quantity, limit_price, stop_price, take_profit, filled_price, filled_qty, "
                "slippage_pct, fee_paid, state, exchange_id, kelly_fraction, risk_usd, "
                "reject_reason, close_reason, realized_pnl, created_at, closed_at "
                "FROM orders WHERE state='MONITORING'"
            )
        for row in rows:
            lp = float(row["limit_price"] or 0)
            rec = OrderRecord(
                id=str(row["id"]),
                signal_id=str(row["signal_id"]) if row["signal_id"] else "",
                symbol=row["symbol"],
                exchange=row["exchange"],
                side=row["side"],
                order_type=row["order_type"],
                quantity=float(row["quantity"] or 0),
                limit_price=lp or None,
                entry_price=lp,
                stop_loss=float(row["stop_price"] or 0),
                take_profit=float(
                    row["take_profit"] or 0
                ),  # restored so TP exits survive restart (item #7)
                filled_price=float(row["filled_price"] or 0),
                filled_qty=float(row["filled_qty"] or 0),
                slippage_pct=float(row["slippage_pct"] or 0),
                fee_paid=float(row["fee_paid"] or 0),
                state=OrderState.MONITORING,
                exchange_id=row["exchange_id"] or "",
                kelly_fraction=float(row["kelly_fraction"] or 0),
                risk_usd=float(row["risk_usd"] or 0),
                reject_reason=row["reject_reason"] or "",
                close_reason=row["close_reason"] or "",
                realized_pnl=float(row["realized_pnl"])
                if row["realized_pnl"] is not None
                else None,
                created_at=row["created_at"],
                closed_at=row["closed_at"],
            )
            if rec.exchange_id:
                _exchange_map[rec.exchange_id] = rec.id
            fsm = OrderFSM(rec)
            _orders[rec.id] = fsm
            _recent_orders.append(fsm)
        logger.info(f"recovered {len(rows)} MONITORING orders from DB")
    except Exception as e:
        logger.error(f"DB order recovery failed: {e}")


LIVE_EXEC_MODES = ("AUTO_EXEC", "CONFIRM_THEN_EXEC")


def _assert_safe_exec_mode():
    """Fail-fast guard: refuse to place orders against a live (production) exchange unless an
    operator has explicitly acknowledged it AND the strategy has out-of-sample evidence.

    Both AUTO_EXEC and CONFIRM_THEN_EXEC reach mainnet (the latter after a manual
    /orders/{id}/confirm), so both are gated (item #7). Two independent acks are required:

      - I_UNDERSTAND_LIVE_AUTO_EXEC=yes  — operator understands this trades real money.
      - I_HAVE_OOS_EVIDENCE=yes          — "guilty until proven innocent" (audit P0-5): the
        deployed strategy has PASSED out-of-sample validation (run the real-strategy
        backtest, enforce_oos_gate must pass). Without proven OOS evidence the system has no
        business risking capital, so a green backtest verdict is now *binding* at the live
        boundary, not a number nobody reads.

    Defaults (NOTIFY_ONLY / development) require neither, so paper/observe stays frictionless."""
    env = os.getenv("ENVIRONMENT", "development")
    if EXEC_MODE in LIVE_EXEC_MODES and env == "production":
        ack = os.getenv("I_UNDERSTAND_LIVE_AUTO_EXEC", "").lower() in (
            "1",
            "true",
            "yes",
        )
        if not ack:
            raise RuntimeError(
                f"Refusing to start: EXEC_MODE={EXEC_MODE} with ENVIRONMENT=production. "
                "This would place orders against real funds. "
                "Set I_UNDERSTAND_LIVE_AUTO_EXEC=yes to override."
            )
        oos = os.getenv("I_HAVE_OOS_EVIDENCE", "").lower() in ("1", "true", "yes")
        if not oos:
            raise RuntimeError(
                f"Refusing to start: EXEC_MODE={EXEC_MODE} with ENVIRONMENT=production but no "
                "out-of-sample evidence asserted. The deployed strategy must PASS the real-strategy "
                "backtest (backtest.v2_strategy_backtest.enforce_oos_gate) before risking capital — "
                "guilty until proven innocent. Set I_HAVE_OOS_EVIDENCE=yes once it has."
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    _assert_safe_exec_mode()
    _init_adapters()
    # 交易所 fill WS 只在 live 模式有意义:PAPER/NOTIFY_ONLY 不向交易所下单,
    # 永远等不到回报,订阅只会无限重连刷错误日志(OKX 全局不可达,每 5-60s 一条)。
    if EXEC_MODE in LIVE_EXEC_MODES:
        for name, adp in get_all_adapters().items():
            adp.on_fill(on_fill)
            asyncio.create_task(adp.subscribe_fills())
            logger.info(f"Fill subscription: {name}")
    else:
        logger.info(f"EXEC_MODE={EXEC_MODE}: exchange fill subscription skipped")
    await _recover_monitoring_orders()
    # run_forever:Redis 断连不再永久杀死消费/心跳任务(2026-07-12 事故;reconcile_loop
    # 写 deadman 心跳,它一死看门狗就 trip cw4:execution:halt——07-10 停单根因)
    tasks = [
        asyncio.create_task(run_forever("consume_loop", consume_loop)),
        asyncio.create_task(run_forever("monitoring_loop", monitoring_loop)),
        asyncio.create_task(run_forever("reconcile_loop", reconcile_loop)),
    ]
    yield
    for t in tasks:
        t.cancel()
    for adp in get_all_adapters().values():
        await adp.close()


app = FastAPI(title="CryptoWatch v4 Execution Service", lifespan=lifespan)


# ── Prometheus metrics (item #12) ───────────────────────────────────────────────
@app.get("/metrics")
async def metrics():
    """Prometheus exposition: service liveness + redis reachability.
    Scraped by the central observability stack (Prometheus/Grafana)."""
    from fastapi.responses import PlainTextResponse
    from shared.metrics import render_prometheus

    out = [
        {
            "name": "selene_up",
            "value": 1,
            "labels": {"service": "execution"},
            "help": "service process is up",
        }
    ]
    try:
        from shared.db.connections import redis_health

        out.append(
            {
                "name": "selene_redis_up",
                "value": await redis_health(),
                "labels": {"service": "execution"},
                "help": "redis reachable",
            }
        )
    except Exception:
        pass
    return PlainTextResponse(render_prometheus(out))


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "execution",
        "exec_mode": EXEC_MODE,
        "redis": await redis_health(),
        "pg": await pg_health(),
        "stats": _stats,
        "active_orders": len([f for f in _orders.values() if not f.record.is_terminal]),
    }


@app.get("/orders/recent")
async def recent_orders(limit: int = 50):
    return {"orders": [f.to_dict() for f in list(_recent_orders)[-limit:]]}


@app.get("/execution/reconcile")
async def reconcile_status():
    r = await get_redis()
    halt = await r.get("cw4:execution:halt")
    hb = await r.get("cw4:execution:heartbeat")
    return {
        "reconcile": _last_reconcile,
        "halt": json.loads(halt) if halt else None,
        "heartbeat": json.loads(hb) if hb else None,
    }


@app.post("/execution/halt/clear")
async def clear_halt():
    """Clear the execution halt after an operator has manually reconciled exchange positions."""
    r = await get_redis()
    await r.delete("cw4:execution:halt")
    logger.warning("EXECUTION HALT cleared by operator")
    return {"status": "cleared"}


@app.post("/orders/{order_id}/confirm")
async def confirm_order(order_id: str):
    fsm = _orders.get(order_id)
    if not fsm:
        return {"error": "not_found"}
    await submit_to_exchange(fsm)
    return {"status": "submitted", "order_id": order_id}


@app.post("/orders/{order_id}/cancel")
async def cancel_order(order_id: str):
    fsm = _orders.get(order_id)
    if not fsm:
        return {"error": "not_found"}
    rec = fsm.record
    if rec.exchange_id:
        try:
            adp = get_adapter(rec.exchange or PRIMARY_EXCHANGE)
            await adp.cancel_order(rec.symbol, rec.exchange_id)
        except Exception as e:
            logger.warning(f"cancel on exchange: {e}")
    fsm.transition(OrderState.CANCELLED, note="manual_cancel")
    _stats["cancelled"] += 1
    return {"status": "cancelled"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("SERVICE_PORT", 8005)))
