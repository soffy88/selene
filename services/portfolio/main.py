"""
services/portfolio/main.py  —  CryptoWatch v4 Portfolio Service（新建）

修复：
  ✅ 完整主循环（原来缺失）
  ✅ 统一资产视角：合并所有持仓、计算总风险
  ✅ Kelly + Risk Parity 仓位计算写入 ScoredSignal
  ✅ 实时净值更新 → redis cw4:portfolio:state
  ✅ 每5秒发布 portfolio.state stream
"""

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI

from shared.db.connections import get_redis, get_pg, redis_health
from shared.events.streams import (
    STREAM_SIGNAL_SCORED,
    STREAM_SIGNAL_SIZED,
    STREAM_ORDER_LIFECYCLE,
    STREAM_PORTFOLIO_STATE,
    encode,
    decode,
    run_forever,
)
from shared.models.signal import ScoredSignal
from shared.models.portfolio import (
    PortfolioState,
    Position,
    PositionSide,
    PositionStatus,
)
from services.portfolio.capital.kelly import CapitalAllocator

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

CG_PORTFOLIO = "portfolio-service"
INITIAL_CAPITAL = float(os.getenv("INITIAL_CAPITAL", "10000"))
KELLY_FRACTION = float(os.getenv("KELLY_FRACTION", "0.5"))
MAX_SINGLE_POS = float(os.getenv("MAX_SINGLE_POSITION", "0.10"))
TARGET_VOL = float(os.getenv("TARGET_VOLATILITY", "0.20"))
# 成本假设：round-trip = 2×taker fee + expected slippage（默认 0.12%）
ROUND_TRIP_COST = float(os.getenv("ROUND_TRIP_COST", "0.0012"))


class PortfolioEngine:
    def __init__(self):
        self._positions: dict[str, Position] = {}  # signal_id → Position
        self._equity = INITIAL_CAPITAL  # realized equity
        self._last_unrealized = 0.0  # latest mark-to-market open PnL
        self._peak_equity = INITIAL_CAPITAL  # high-water mark on MTM equity
        self._daily_pnl = 0.0
        self._realized_pnl = 0.0
        self._allocator = CapitalAllocator(
            total_equity=INITIAL_CAPITAL,
            kelly_fraction_=KELLY_FRACTION,
            target_volatility=TARGET_VOL,
            max_single_pos_pct=MAX_SINGLE_POS,
            round_trip_cost=ROUND_TRIP_COST,
        )
        # 按策略类型追踪波动率（用于 Risk Parity）
        self._strategy_returns: dict[str, list] = {}

    # ── 处理新信号：计算仓位大小 ───────────────────────
    async def size_signal(self, signal_dict: dict) -> dict:
        """
        接收 signal.scored，计算 Kelly 仓位大小后，
        写回 allocated_capital / position_size / risk_usd 字段，
        重新发布到 signal.scored（execution-service 消费）。
        """
        try:
            signal = ScoredSignal.from_dict(signal_dict)
        except Exception as e:
            logger.error(f"ScoredSignal parse: {e}")
            return signal_dict

        # Size against mark-to-market equity (incl. open PnL), not realized-only,
        # so sizing shrinks when we're in open drawdown (item #5).
        self._allocator.update_equity(self.equity_mtm)

        strategy = signal.signal_type.value

        # 更新策略波动率（用已实现收益近似）
        ret_history = self._strategy_returns.get(strategy, [])
        if ret_history:
            import math

            mean = sum(ret_history) / len(ret_history)
            vol = math.sqrt(
                sum((r - mean) ** 2 for r in ret_history) / len(ret_history)
            )
            self._allocator.update_strategy_volatility(
                strategy, vol * 16
            )  # 日化 → 年化

        # Perpetual funding drag over the expected hold feeds the cost-adjusted Kelly.
        funding_rate = None
        try:
            fr = signal.indicators.get("funding_rate")
            funding_rate = float(fr) if fr is not None else None
        except Exception:
            funding_rate = None

        sizing = self._allocator.compute_position_size(
            strategy=strategy,
            win_probability=signal.win_probability,
            risk_reward=signal.risk_reward,
            entry_price=signal.entry_price,
            stop_price=signal.stop_loss,
            drawdown_scalar=self._drawdown_scalar(),
            funding_rate=funding_rate,
            side=signal.direction.value,
            hold_hours=float(signal.max_hold_hours or 24),
        )

        signal_dict["allocated_capital"] = sizing["notional_usd"]
        signal_dict["position_size"] = sizing["quantity"]
        signal_dict["risk_usd"] = sizing["risk_usd"]
        signal_dict["kelly_fraction"] = sizing["kelly_fraction"]
        signal_dict["strategy_alloc"] = sizing["strategy_alloc"]

        logger.info(
            f"SIZED {signal.symbol} {signal.signal_type.value} "
            f"kelly={sizing['kelly_fraction']:.3f} "
            f"qty={sizing['quantity']:.6f} "
            f"notional=${sizing['notional_usd']:.2f} "
            f"risk=${sizing['risk_usd']:.2f}"
        )
        return signal_dict

    # ── 处理订单生命周期事件 ───────────────────────────
    async def handle_order_event(self, data: dict):
        event = data.get("event", "")
        signal_id = data.get("signal_id", "")
        symbol = data.get("symbol", "")

        # PAPER 撮合发 "filled_immediately",live 交易所回报发 "filled"——两者都是建仓。
        # 此前只认 "filled",PAPER 模式下 portfolio 永远收不到建仓事件、持仓视图恒空,
        # 只靠启动时 DB 恢复(07-15 HOME/BTC 重复仓对账失效即此因)。
        if event in ("filled", "filled_immediately"):
            # 建仓
            side = (
                PositionSide.LONG if data.get("side") == "BUY" else PositionSide.SHORT
            )
            pos = Position(
                id=data.get("id", ""),
                symbol=symbol,
                side=side,
                entry_price=float(data.get("filled_price", 0)),
                quantity=float(data.get("filled_qty", 0)),
                stop_loss=float(data.get("stop_loss", 0)),
                take_profit=float(data.get("take_profit", 0) or 0),
                strategy=data.get("signal_type", ""),
                signal_id=signal_id,
            )
            self._positions[signal_id] = pos
            logger.info(f"Position OPENED {symbol} {side.value} qty={pos.quantity}")

        elif event == "closed":
            pos = self._positions.pop(signal_id, None)
            if pos:
                pnl = float(data.get("realized_pnl", 0))
                pos.realized_pnl = pnl
                pos.status = PositionStatus.CLOSED
                self._realized_pnl += pnl
                self._daily_pnl += pnl
                self._equity += pnl
                self._peak_equity = max(self._peak_equity, self._equity)
                # 记录策略收益（用于 Risk Parity 波动率更新）
                strategy = pos.strategy
                if pos.entry_price > 0:
                    ret = pnl / (pos.entry_price * pos.quantity)
                    self._strategy_returns.setdefault(strategy, []).append(ret)
                    if len(self._strategy_returns[strategy]) > 100:
                        self._strategy_returns[strategy].pop(0)
                logger.info(f"Position CLOSED {symbol} pnl={pnl:+.4f}")

    @property
    def equity_mtm(self) -> float:
        """Mark-to-market equity = realized equity + latest open PnL."""
        return self._equity + self._last_unrealized

    # ── 更新持仓浮盈 ──────────────────────────────────
    async def mark_to_market(self, prices: dict):
        total_unrealized = 0.0
        for pos in self._positions.values():
            price = prices.get(pos.symbol, 0)
            if price:
                pos.update_unrealized(price)
                total_unrealized += pos.unrealized_pnl
        self._last_unrealized = total_unrealized
        # High-water mark tracks MTM equity so open drawdown is captured.
        self._peak_equity = max(self._peak_equity, self.equity_mtm)
        return total_unrealized

    def _var_es_usd(self, exposure: float) -> tuple[float, float]:
        """Historical 95% VaR and Expected Shortfall (CVaR) in USD from the pooled
        realized strategy-return distribution, scaled by current exposure (item #17).
        Returns (0,0) until enough return history exists."""
        pooled = [r for rs in self._strategy_returns.values() for r in rs]
        if len(pooled) < 20 or exposure <= 0:
            return 0.0, 0.0
        losses = sorted(pooled)  # ascending; left tail = worst losses
        k = max(1, int(len(losses) * 0.05))
        tail = losses[:k]
        var_ret = -losses[k - 1]  # 95% VaR as a positive loss fraction
        es_ret = -(sum(tail) / len(tail))  # CVaR = mean of the worst 5%
        return max(0.0, var_ret) * exposure, max(0.0, es_ret) * exposure

    # ── 构建 PortfolioState ───────────────────────────
    def build_state(self, unrealized: float) -> PortfolioState:
        total_exposure = sum(
            pos.entry_price * pos.quantity for pos in self._positions.values()
        )
        mtm_equity = self._equity + unrealized
        leverage = total_exposure / mtm_equity if mtm_equity > 0 else 0

        # Drawdown measured on MTM equity vs the high-water mark (item #5).
        drawdown = (
            (self._peak_equity - mtm_equity) / self._peak_equity
            if self._peak_equity > 0
            else 0.0
        )
        drawdown = max(drawdown, 0.0)

        # Risk fields that were previously left at 0 (item #17).
        largest_notional = max(
            (pos.entry_price * pos.quantity for pos in self._positions.values()),
            default=0.0,
        )
        largest_position_pct = largest_notional / mtm_equity if mtm_equity > 0 else 0.0
        var_95, es = self._var_es_usd(total_exposure)

        state = PortfolioState(
            total_equity=round(self._equity + unrealized, 2),
            available_capital=round(self._equity - total_exposure + unrealized, 2),
            total_exposure=round(total_exposure, 2),
            leverage=round(leverage, 3),
            open_positions=[p.to_dict() for p in self._positions.values()],
            position_count=len(self._positions),
            current_drawdown=round(drawdown, 4),
            max_drawdown=round(max(drawdown, 0), 4),
            largest_position_pct=round(largest_position_pct, 4),
            portfolio_var_95=round(var_95, 2),
            expected_shortfall=round(es, 2),
            daily_pnl=round(self._daily_pnl, 2),
            total_pnl=round(self._realized_pnl + unrealized, 2),
            strategy_allocations=self._allocator.get_allocations().get("weights", {}),
        )
        return state

    def _drawdown_scalar(self) -> float:
        if self._peak_equity <= 0:
            return 1.0
        # Use MTM equity so deep open drawdown throttles new sizing (item #5).
        dd = (self._peak_equity - self.equity_mtm) / self._peak_equity
        if dd < 0.05:
            return 1.00
        elif dd < 0.10:
            return 0.50
        elif dd < 0.15:
            return 0.25
        else:
            return 0.00


_engine = PortfolioEngine()


async def consume_loop():
    r = await get_redis()

    for stream in [STREAM_SIGNAL_SCORED, STREAM_ORDER_LIFECYCLE]:
        try:
            await r.xgroup_create(stream, CG_PORTFOLIO, id="0", mkstream=True)
        except Exception as e:
            if "BUSYGROUP" not in str(e):
                raise

    logger.info("Portfolio service ready")

    while True:
        # 消费 signal.scored → 计算仓位大小 → 写入 signal.sized（独立流防重复下单）
        sig_results = await r.xreadgroup(
            CG_PORTFOLIO,
            "portfolio-signals",
            {STREAM_SIGNAL_SCORED: ">"},
            count=10,
            block=500,
        )
        for _, messages in sig_results or []:
            for msg_id, fields in messages:
                try:
                    data = decode(fields)
                    sized = await _engine.size_signal(data)
                    # 只有有效 sizing（quantity > 0）才发布到 execution 流
                    if sized.get("position_size", 0) > 0:
                        await r.xadd(
                            STREAM_SIGNAL_SIZED,
                            encode(sized),
                            maxlen=10000,
                            approximate=True,
                        )
                    else:
                        logger.info(
                            f"DROP zero-sized {sized.get('symbol')} "
                            f"kelly={sized.get('kelly_fraction')}"
                        )
                    await r.xack(STREAM_SIGNAL_SCORED, CG_PORTFOLIO, msg_id)
                except Exception as e:
                    logger.error(f"size_signal: {e}", exc_info=True)

        # 消费订单事件 → 更新持仓
        ord_results = await r.xreadgroup(
            CG_PORTFOLIO,
            "portfolio-orders",
            {STREAM_ORDER_LIFECYCLE: ">"},
            count=20,
            block=500,
        )
        for _, messages in ord_results or []:
            for msg_id, fields in messages:
                try:
                    await _engine.handle_order_event(decode(fields))
                    await r.xack(STREAM_ORDER_LIFECYCLE, CG_PORTFOLIO, msg_id)
                except Exception as e:
                    logger.error(f"order_event: {e}", exc_info=True)


async def state_publisher():
    """每5秒发布 portfolio.state + 更新 Redis 缓存"""
    r = await get_redis()
    while True:
        await asyncio.sleep(5)
        try:
            # 取价格
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

            unrealized = await _engine.mark_to_market(prices)
            state = _engine.build_state(unrealized)
            state_dict = state.to_dict()

            await r.set("cw4:portfolio:state", json.dumps(state_dict))
            await r.xadd(
                STREAM_PORTFOLIO_STATE,
                encode(state_dict),
                maxlen=10000,
                approximate=True,
            )

        except Exception as e:
            logger.error(f"state_publisher: {e}")


async def _recover_portfolio():
    """On startup: rebuild _positions from MONITORING orders + accumulated realized PnL from DB."""
    try:
        pool = await get_pg()
        r = await get_redis()
        # Determine session start: Redis key → fallback 2026-04-30 00:00 UTC
        sess_raw = await r.get("cw4:portfolio:session_start")
        if sess_raw:
            session_start = datetime.fromisoformat(
                sess_raw.decode() if isinstance(sess_raw, bytes) else sess_raw
            )
        else:
            session_start = datetime(2026, 4, 30, 0, 0, 0)

        async with pool.acquire() as conn:
            # Rebuild open positions from MONITORING orders
            mon_rows = await conn.fetch(
                "SELECT id, signal_id, symbol, side, filled_price, filled_qty, "
                "stop_price, created_at "
                "FROM orders WHERE state='MONITORING'"
            )
            for row in mon_rows:
                pos_side = (
                    PositionSide.LONG if row["side"] == "BUY" else PositionSide.SHORT
                )
                signal_id = (
                    str(row["signal_id"]) if row["signal_id"] else str(row["id"])
                )
                pos = Position(
                    id=str(row["id"]),
                    symbol=row["symbol"],
                    side=pos_side,
                    entry_price=float(row["filled_price"] or 0),
                    quantity=float(row["filled_qty"] or 0),
                    stop_loss=float(row["stop_price"] or 0),
                    take_profit=0.0,
                    signal_id=signal_id,
                    opened_at=row["created_at"],
                )
                _engine._positions[signal_id] = pos

            # Accumulate realized PnL from CLOSED orders since session_start
            pnl_row = await conn.fetchrow(
                "SELECT COALESCE(SUM(realized_pnl), 0) AS total_pnl "
                "FROM orders WHERE state='CLOSED' AND created_at >= $1",
                session_start,
            )
            realized = float(pnl_row["total_pnl"] or 0)

        _engine._realized_pnl = realized
        _engine._daily_pnl = realized
        _engine._equity = INITIAL_CAPITAL + realized
        _engine._peak_equity = max(INITIAL_CAPITAL, _engine._equity)
        logger.info(
            f"recovered {len(mon_rows)} positions, realized_pnl={realized:+.4f}, equity={_engine._equity:.2f}"
        )
    except Exception as e:
        logger.error(f"portfolio DB recovery failed: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await _recover_portfolio()
    # run_forever:Redis 断连不再永久杀死消费任务(2026-07-12 事故)
    tasks = [
        asyncio.create_task(run_forever("consume_loop", consume_loop)),
        asyncio.create_task(run_forever("state_publisher", state_publisher)),
    ]
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="CryptoWatch v4 Portfolio Service", lifespan=lifespan)


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
            "labels": {"service": "portfolio"},
            "help": "service process is up",
        }
    ]
    try:
        from shared.db.connections import redis_health

        out.append(
            {
                "name": "selene_redis_up",
                "value": await redis_health(),
                "labels": {"service": "portfolio"},
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
        "service": "portfolio",
        "redis": await redis_health(),
        "equity": _engine._equity,
        "positions": len(_engine._positions),
        "daily_pnl": _engine._daily_pnl,
    }


@app.get("/state")
async def state():
    return _engine.build_state(0).to_dict()


@app.get("/allocations")
async def allocations():
    return _engine._allocator.get_allocations()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("SERVICE_PORT", 8003)))
