"""
services/risk/main.py  —  CryptoWatch v4 Risk Service（强制约束型）

修复：
  ✅ 完整主循环（原来缺失）
  ✅ 强制 Gate：VaR 超限 / Drawdown RED → 直接拒单，不是建议
  ✅ 相关性检查：防止同方向暴露叠加
  ✅ 每日亏损熔断（max_daily_loss）
  ✅ 仓位压缩（drawdown_scalar 随 drawdown 级别自动收缩）
  ✅ 每5秒发布风控状态到 Redis 供 gateway + execution 读取
"""

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI

from shared.db.connections import get_redis, get_pg, redis_health
from services.risk.persistence.circuit_breaker import PersistentCircuitBreaker
from shared.events.streams import (
    STREAM_RISK_CHECK, STREAM_RISK_APPROVED, STREAM_SYSTEM_ALERTS,
    encode, decode,
)
from services.risk.portfolio.var_engine import (
    calc_historical_var, DrawdownController, DRAWDOWN_LEVELS,
)
from services.risk.portfolio.correlation_risk import (
    same_direction_correlated_exposure, parametric_var_correlated)

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

CG_RISK = "risk-service"

# ── 风控参数（从 .env 读）────────────────────────────
MAX_PORTFOLIO_DRAWDOWN = float(os.getenv("MAX_PORTFOLIO_DRAWDOWN", "0.15"))
TARGET_VOLATILITY      = float(os.getenv("TARGET_VOLATILITY",      "0.20"))
MAX_SINGLE_POS_PCT     = float(os.getenv("MAX_SINGLE_POSITION",    "0.10"))
MAX_LEVERAGE           = float(os.getenv("MAX_LEVERAGE",           "3.0"))
MAX_DAILY_LOSS_PCT     = float(os.getenv("MAX_DAILY_LOSS_PCT",     "0.05"))   # 5% 日亏损熔断
MAX_CORR_EXPOSURE      = float(os.getenv("MAX_CORR_EXPOSURE",      "0.30"))   # 同向暴露上限30%
MIN_WIN_PROBABILITY    = float(os.getenv("MIN_WIN_PROBABILITY",    "0.55"))
VAR_LIMIT_PCT          = float(os.getenv("VAR_LIMIT_PCT",          "0.05"))   # post-trade VaR ≤ 5% of equity
# Perp liquidation guard (the existential gate for a leveraged perpetuals account).
MAINT_MARGIN_RATE      = float(os.getenv("MAINT_MARGIN_RATE",      "0.005"))  # exchange maintenance-margin rate (BTC perp low tiers ≈ 0.4–0.5%)
MIN_LIQ_BUFFER_PCT     = float(os.getenv("MIN_LIQ_BUFFER_PCT",     "0.10"))   # post-trade price must sit ≥ this far (fraction) from the liquidation price
STOP_LIQ_SAFETY        = float(os.getenv("STOP_LIQ_SAFETY",        "0.80"))   # protective stop must fall within this fraction of the liquidation distance (so it triggers BEFORE liquidation)
# Dynamic correlation gate (replaces the static CORR_GROUPS when return data is available).
CORR_THRESHOLD         = float(os.getenv("CORR_THRESHOLD",         "0.6"))    # signed ρ ≥ this ⇒ correlated (same-direction clustering)
CORR_RETURNS_TTL_S     = float(os.getenv("CORR_RETURNS_TTL_S",     "300"))    # cache returns for 5 min
CORR_RETURNS_INTERVAL  = os.getenv("CORR_RETURNS_INTERVAL",        "1h")
CORR_RETURNS_BARS      = int(os.getenv("CORR_RETURNS_BARS",        "120"))
CORR_MIN_BARS          = int(os.getenv("CORR_MIN_BARS",            "30"))     # min bars to trust ρ

_corr_returns_cache: dict = {"ts": 0.0, "data": {}}


async def _get_corr_returns(symbols: set) -> dict:
    """Per-symbol log-return series from the candles table, cached for CORR_RETURNS_TTL_S.
    Returns {symbol: [log_returns]} for the symbols that have enough history."""
    import math
    now = time.time()
    cache = _corr_returns_cache
    if now - cache["ts"] < CORR_RETURNS_TTL_S and set(symbols) <= set(cache["data"]):
        return {s: cache["data"][s] for s in symbols if s in cache["data"]}
    out: dict = {}
    try:
        pg = await get_pg()
        async with pg.acquire() as conn:
            for s in symbols:
                rows = await conn.fetch(
                    "SELECT close FROM candles WHERE symbol=$1 AND interval=$2 "
                    "ORDER BY time DESC LIMIT $3",
                    s, CORR_RETURNS_INTERVAL, CORR_RETURNS_BARS + 1)
                closes = [float(r["close"]) for r in rows][::-1]   # ascending
                if len(closes) >= CORR_MIN_BARS:
                    rets = [math.log(closes[i] / closes[i - 1])
                            for i in range(1, len(closes)) if closes[i - 1] > 0]
                    if rets:
                        out[s] = rets
    except Exception as e:
        logger.warning(f"corr returns fetch failed: {e}")
        return {}
    cache["ts"] = now
    cache["data"] = out
    return out


def _signed_positions() -> dict:
    """Open positions as {symbol: signed notional} (+long / -short), excluding the equity marker."""
    out = {}
    for s, p in _open_positions.items():
        if s == "__equity__" or "notional" not in p:
            continue
        sign = 1.0 if str(p.get("side", "LONG")).upper() in ("BUY", "LONG") else -1.0
        out[s] = sign * float(p.get("notional", 0.0))
    return out


async def compute_portfolio_risk() -> dict:
    """Correlation-aware portfolio risk snapshot: asset-level VaR (w'Σw), correlated
    same-direction exposure, and standard stress scenarios.

    Surfaced for monitoring / risk oversight — NOT an order gate: its per-bar horizon differs
    from the equity-series VaR gate, and a hard threshold here would be easy to miscalibrate.
    It closes the 'VaR ignores cross-asset correlation' gap by exposing the correct number.
    """
    from services.risk.portfolio.correlation_risk import (
        parametric_var_correlated, correlated_exposure, stress_test)
    equity = _get_equity()
    signed = _signed_positions()
    if not signed:
        return {"equity": round(equity, 2), "positions": 0, "var_1bar_usd": 0.0,
                "var_1bar_pct": 0.0, "max_correlated_exposure_pct": 0.0, "stress": {}}
    returns = await _get_corr_returns(set(signed))
    var_usd = parametric_var_correlated(signed, returns, confidence=0.95) if returns else 0.0
    corr_exp = correlated_exposure(signed, returns, threshold=CORR_THRESHOLD) if returns else {}
    held = list(signed)
    scenarios = {
        "broad_drop_15":  {s: -0.15 for s in held},
        "broad_rally_15": {s:  0.15 for s in held},
        "btc_led_crash":  {s: (-0.20 if s == "BTCUSDT" else -0.12) for s in held},
    }
    stress = stress_test(signed, scenarios)
    return {
        "equity": round(equity, 2),
        "positions": len(signed),
        "var_1bar_usd": round(var_usd, 2),
        "var_1bar_pct": round(var_usd / equity, 4) if equity > 0 else 0.0,
        "max_correlated_exposure_pct": round(corr_exp.get("max_fraction", 0.0), 4),
        "worst_stress_usd": round(min(stress.values()), 2) if stress else 0.0,
        "stress": stress,
    }

# 已知高相关资产组（防止 all-in 同一风险）
CORR_GROUPS = [
    {"BTCUSDT", "ETHUSDT", "SOLUSDT", "AVAXUSDT"},   # 主流 L1
    {"DOGEUSDT", "SHIBUSDT", "PEPEUSDT", "BONKUSDT"}, # Meme
    {"LDOUSDT", "RPLUSDT", "SSVUSDT"},                # 质押衍生品
]

_dd_controller   = DrawdownController()
_daily_pnl:  float = 0.0
_daily_reset_date: str = datetime.now(timezone.utc).date().isoformat()
_circuit_broken: bool = False   # 内存状态（向后兼容）
_circuit_reason: str  = ""
_open_positions: dict = {}   # symbol → {side, notional}
_equity_history: list = []   # 用于 VaR 计算
_cb: PersistentCircuitBreaker = None   # 持久化熔断器（lifespan 初始化）


class RiskGate:
    """
    强制约束型风控。
    approve() 返回 (approved: bool, reason: str, max_qty: float | None)
    """

    async def check_circuit_breaker(self) -> tuple[bool, str]:
        """熔断检查：优先读持久化状态，回退到内存状态"""
        global _circuit_broken, _circuit_reason
        # 优先读持久化熔断器
        if _cb is not None:
            is_open = await _cb.is_open()
            if is_open:
                state = await _cb.get_state()
                return False, f"circuit_broken: {state.get('circuit_reason', '未知原因')}"
        # 回退到内存状态（兼容）
        if _circuit_broken:
            return False, f"circuit_broken: {_circuit_reason}"
        return True, ""

    def check_drawdown_level(self) -> tuple[bool, str, float]:
        """
        Drawdown Gate：
          GREEN  → 允许，仓位系数 1.0
          YELLOW → 允许，仓位系数 0.5
          ORANGE → 允许，仓位系数 0.25
          RED    → 拒绝所有新仓
        """
        level = _dd_controller.level
        scalar = level.max_position_pct
        if not level.new_trades_allowed:
            return False, f"drawdown_halt level={level.name} dd={_dd_controller.current_dd:.1%}", 0.0
        return True, "", scalar

    async def check_var(self, symbol: str, side: str, allocated_usd: float, equity: float) -> tuple[bool, str]:
        """VaR Gate: post-trade portfolio VaR must not exceed VAR_LIMIT_PCT of equity.

        Primary path uses the correlation-aware portfolio VaR (w'Σw) including the
        candidate position, so cross-asset correlation is captured — replacing the
        prior arbitrary `var_pct + allocated/equity*0.15` estimate (item #9).
        Falls back to the equity-series historical VaR (scaled by the exposure the
        candidate adds) when per-symbol return data isn't available yet."""
        if equity <= 0:
            return True, ""

        # ── Primary: correlated post-trade VaR ──
        signed = _signed_positions()
        sign = 1.0 if str(side).upper() in ("BUY", "LONG") else -1.0
        signed[symbol] = signed.get(symbol, 0.0) + sign * allocated_usd
        returns = await _get_corr_returns(set(signed))
        if returns:
            var_usd = parametric_var_correlated(signed, returns, confidence=0.95)
            var_pct = var_usd / equity if equity > 0 else 0.0
            if var_pct > VAR_LIMIT_PCT:
                return False, f"VaR limit: post-trade {var_pct:.1%} > {VAR_LIMIT_PCT:.0%} (correlated)"
            return True, ""

        # ── Fallback: equity-series historical VaR scaled by added exposure ──
        if len(_equity_history) < 30:
            return True, ""   # insufficient data
        eq_returns = [
            (_equity_history[i] - _equity_history[i-1]) / _equity_history[i-1]
            for i in range(1, len(_equity_history))
            if _equity_history[i-1] > 0
        ]
        var_result = calc_historical_var([r * equity for r in eq_returns])
        if not var_result:
            return True, ""
        var_pct = var_result.var_95 / equity if equity > 0 else 0
        # The new notional scales portfolio risk roughly in proportion to the
        # exposure it adds (no arbitrary constant).
        new_var_est = var_pct * (1.0 + allocated_usd / equity)
        if new_var_est > VAR_LIMIT_PCT:
            return False, f"VaR limit: est {new_var_est:.1%} > {VAR_LIMIT_PCT:.0%}"
        return True, ""

    def check_single_position(
        self, symbol: str, allocated_usd: float, equity: float
    ) -> tuple[bool, str, float]:
        """单仓位上限"""
        pos_pct = allocated_usd / equity if equity > 0 else 1.0
        if pos_pct > MAX_SINGLE_POS_PCT:
            max_allowed = equity * MAX_SINGLE_POS_PCT
            return True, f"position capped to {MAX_SINGLE_POS_PCT:.0%}", max_allowed
        return True, "", allocated_usd

    def check_corr_exposure(self, symbol: str, side: str) -> tuple[bool, str]:
        """
        相关性暴露检查：
        同一 corr group 内，同方向已有仓位 notional 之和超过 MAX_CORR_EXPOSURE × equity → 拒绝
        """
        # 找到该 symbol 所属的相关组
        group = None
        for g in CORR_GROUPS:
            if symbol in g:
                group = g
                break
        if not group:
            return True, ""

        # 计算该组同方向暴露。
        # Normalise side to a sign before comparing: stored positions carry "LONG"/"SHORT"
        # while the incoming order side is "BUY"/"SELL", so a raw == comparison never matched
        # and this static fallback silently passed everything at cold start (audit P1-5). Use
        # the same normalisation as the dynamic path (correlation_risk.same_direction...).
        def _sign(s: str) -> int:
            return 1 if str(s).upper() in ("BUY", "LONG") else -1
        cand_sign = _sign(side)
        same_dir_notional = sum(
            pos["notional"]
            for sym, pos in _open_positions.items()
            if sym in group and _sign(pos.get("side", "LONG")) == cand_sign
        )
        equity = _get_equity()
        if equity <= 0:
            return True, ""

        exposure_pct = same_dir_notional / equity
        if exposure_pct > MAX_CORR_EXPOSURE:
            return False, (
                f"corr_exposure limit: {symbol} group already "
                f"{exposure_pct:.1%} > {MAX_CORR_EXPOSURE:.1%}"
            )
        return True, ""

    async def check_corr_exposure_dynamic(
        self, symbol: str, side: str, allocated: float
    ) -> tuple[bool, str]:
        """Correlation-exposure gate using *realized* correlations instead of the static
        CORR_GROUPS. Sums the candidate plus existing same-direction positions whose realized
        correlation with the candidate >= CORR_THRESHOLD and rejects if that exceeds
        MAX_CORR_EXPOSURE × equity. Falls back to the static group check when return data
        is unavailable (cold start), so the gate never silently weakens."""
        equity = _get_equity()
        if equity <= 0:
            return True, ""
        cand_sign = 1 if str(side).upper() in ("BUY", "LONG") else -1
        positions = {s: p for s, p in _open_positions.items()
                     if s != "__equity__" and "notional" in p}
        universe = set(positions) | {symbol}
        returns = await _get_corr_returns(universe)
        cluster_notional = same_direction_correlated_exposure(
            symbol, cand_sign, allocated, positions, returns, threshold=CORR_THRESHOLD)
        if cluster_notional is None:
            # no realized-correlation data for the candidate → static fallback
            return self.check_corr_exposure(symbol, side)
        exposure_pct = cluster_notional / equity
        if exposure_pct > MAX_CORR_EXPOSURE:
            return False, (
                f"corr_exposure(dynamic): {symbol} correlated same-dir "
                f"{exposure_pct:.1%} > {MAX_CORR_EXPOSURE:.1%}"
            )
        return True, ""

    def check_win_probability(self, win_prob: float) -> tuple[bool, str]:
        if win_prob < MIN_WIN_PROBABILITY:
            return False, f"win_prob {win_prob:.0%} < threshold {MIN_WIN_PROBABILITY:.0%}"
        return True, ""

    def check_leverage(self, allocated_usd: float, equity: float) -> tuple[bool, str]:
        total_exposure = sum(p["notional"] for p in _open_positions.values() if "notional" in p)
        new_leverage   = (total_exposure + allocated_usd) / equity if equity > 0 else 999
        if new_leverage > MAX_LEVERAGE:
            return False, f"leverage {new_leverage:.2f}x > max {MAX_LEVERAGE}x"
        return True, ""

    def check_liquidation_distance(
        self, allocated_usd: float, entry_price: float, stop_price: float, equity: float
    ) -> tuple[bool, str]:
        """Perp liquidation guard — the existential gate a leveraged perpetuals account needs.

        Models the *post-trade cross-margin* liquidation distance (as a fraction of price)
        from equity and total notional, using the same cross leverage = total_notional/equity
        convention as ``check_leverage``:

            liq_dist ≈ 1/leverage − maintenance_margin_rate

        Rejects when either:
          (a) the position would sit closer to its liquidation price than MIN_LIQ_BUFFER_PCT
              (a leverage backstop independent of MAX_LEVERAGE), or
          (b) the protective stop is at/beyond the liquidation price — i.e. the position
              would be force-liquidated *before* the stop could fill. This is the exact ruin
              mode where a synthetic/exchange stop never gets the chance to work.

        Missing prices ⇒ the stop check is skipped (we can't assert it), but the leverage
        backstop (a) still applies. Fails closed only on a concrete, computable violation.
        """
        if equity <= 0 or allocated_usd <= 0:
            return True, ""
        total_exposure = sum(p["notional"] for p in _open_positions.values() if "notional" in p)
        lev = (total_exposure + allocated_usd) / equity
        if lev <= 0:
            return True, ""
        liq_dist = 1.0 / lev - MAINT_MARGIN_RATE
        if liq_dist <= MIN_LIQ_BUFFER_PCT:
            return False, (
                f"liq_distance {liq_dist:.1%} ≤ min buffer {MIN_LIQ_BUFFER_PCT:.0%} "
                f"(post-trade {lev:.2f}x sits too close to liquidation)"
            )
        if entry_price > 0 and stop_price > 0:
            stop_dist = abs(entry_price - stop_price) / entry_price
            if stop_dist >= liq_dist * STOP_LIQ_SAFETY:
                return False, (
                    f"stop_dist {stop_dist:.1%} ≥ {STOP_LIQ_SAFETY:.0%}×liq_dist {liq_dist:.1%}: "
                    f"protective stop sits at/beyond liquidation — position would liquidate first"
                )
        return True, ""

    async def approve(self, order_data: dict) -> tuple[bool, str, float | None]:
        """
        完整风控检查流水线。
        返回 (approved, reason, max_quantity)
        """
        symbol      = order_data.get("symbol", "")
        side        = order_data.get("side", "BUY")
        allocated   = float(order_data.get("allocated_usd", 0))
        win_prob    = float(order_data.get("win_probability", 0))
        quantity    = float(order_data.get("quantity", 0))
        entry_price = float(order_data.get("entry_price", 0))
        stop_price  = float(order_data.get("stop_price", 0))

        equity = _get_equity()

        # ── Gate 1: 熔断（持久化）──
        ok, reason = await self.check_circuit_breaker()
        if not ok:
            return False, reason, None

        # ── Gate 2: Drawdown 强制压缩 ──
        ok, reason, scalar = self.check_drawdown_level()
        if not ok:
            return False, reason, None
        max_qty = quantity * scalar if scalar < 1.0 else None

        # ── Gate 3: 胜率 ──
        ok, reason = self.check_win_probability(win_prob)
        if not ok:
            return False, reason, None

        # ── Gate 4: 单仓上限 ──
        ok, reason, capped_alloc = self.check_single_position(symbol, allocated, equity)
        if capped_alloc < allocated and capped_alloc > 0:
            # 按比例缩减数量
            ratio   = capped_alloc / allocated if allocated > 0 else 1.0
            max_qty = quantity * ratio * scalar

        # ── Gate 5: 杠杆 ──
        ok, reason = self.check_leverage(allocated, equity)
        if not ok:
            return False, reason, None

        # ── Gate 5b: 强平距离（永续命门：止损必须在强平价之前触发）──
        ok, reason = self.check_liquidation_distance(allocated, entry_price, stop_price, equity)
        if not ok:
            return False, reason, None

        # ── Gate 6: 相关性暴露（动态相关矩阵，数据不足时回退静态分组）──
        # Use the single-position-capped allocation so a lone (uncorrelated) order is bounded by
        # Gate 4, not rejected here — this gate only guards *correlated* concentration.
        eff_alloc = min(allocated, capped_alloc) if capped_alloc > 0 else allocated
        ok, reason = await self.check_corr_exposure_dynamic(symbol, side, eff_alloc)
        if not ok:
            return False, reason, None

        # ── Gate 7: VaR ──
        ok, reason = await self.check_var(symbol, side, allocated, equity)
        if not ok:
            return False, reason, None

        # 全部通过
        return True, "approved", max_qty


_gate = RiskGate()


def _get_equity() -> float:
    """从内存中读取最近净值（由 portfolio state 更新）"""
    return _open_positions.get("__equity__", {}).get("value", 10000.0)


# ── 主消费循环 ────────────────────────────────────────

async def consume_loop():
    r = await get_redis()

    try:
        await r.xgroup_create(STREAM_RISK_CHECK, CG_RISK, id="0", mkstream=True)
    except Exception as e:
        if "BUSYGROUP" not in str(e):
            raise

    logger.info("Risk service ready (HARD GATE MODE)")

    while True:
        results = await r.xreadgroup(
            CG_RISK, "risk-worker", {STREAM_RISK_CHECK: ">"},
            count=10, block=1000,
        )
        for _, messages in (results or []):
            for msg_id, fields in messages:
                try:
                    data = decode(fields)
                    order_id = data.get("order_id", "")
                    approved, reason, max_qty = await _gate.approve(data)

                    response = {
                        "order_id":    order_id,
                        "approved":    approved,
                        "reason":      reason,
                        "max_quantity": max_qty,
                        "checked_at":  datetime.utcnow().isoformat(),
                    }

                    await r.xadd(
                        STREAM_RISK_APPROVED, encode(response),
                        maxlen=10000, approximate=True,
                    )

                    if not approved:
                        logger.warning(f"RISK REJECTED order={order_id[:8]} reason={reason}")
                        # 发 system.alerts 通知
                        await r.xadd(STREAM_SYSTEM_ALERTS, encode({
                            "type":    "risk_alert",
                            "reason":  f"Order rejected: {reason}",
                            "order_id": order_id,
                        }), maxlen=10000, approximate=True)
                    else:
                        logger.info(f"RISK APPROVED order={order_id[:8]} max_qty={max_qty}")

                    await r.xack(STREAM_RISK_CHECK, CG_RISK, msg_id)

                except Exception as e:
                    logger.error(f"risk_check: {e}", exc_info=True)


async def portfolio_state_listener():
    """监听 portfolio state 更新净值、drawdown、持仓"""
    r = await get_redis()
    global _daily_pnl, _daily_reset_date, _circuit_broken, _circuit_reason

    while True:
        await asyncio.sleep(5)
        try:
            raw = await r.get("cw4:portfolio:state")
            if not raw:
                continue
            state = json.loads(raw)
            equity       = float(state.get("total_equity", 0))
            daily_pnl    = float(state.get("daily_pnl",    0))
            positions    = state.get("open_positions",      [])

            if equity > 0:
                _equity_history.append(equity)
                if len(_equity_history) > 500:
                    _equity_history.pop(0)
                _dd_controller.update(equity)
                _open_positions["__equity__"] = {"value": equity}

            # 更新持仓注册表（用于相关性检查）
            _open_positions.clear()
            _open_positions["__equity__"] = {"value": equity}
            for pos in positions:
                _open_positions[pos["symbol"]] = {
                    "side":     pos.get("side", "LONG"),
                    "notional": float(pos.get("notional", 0)),
                }

            # 每日亏损熔断（按 UTC 日期切换，非 86400s 漂移）
            today = datetime.now(timezone.utc).date().isoformat()
            if today != _daily_reset_date:
                _daily_pnl        = 0.0
                _daily_reset_date = today
                logger.info(f"Daily PnL reset at UTC {today}")
            _daily_pnl = daily_pnl

            if equity > 0 and abs(daily_pnl) / equity > MAX_DAILY_LOSS_PCT and daily_pnl < 0:
                trip_reason = f"daily_loss {daily_pnl/equity:.1%} > {MAX_DAILY_LOSS_PCT:.1%}"
                # 持久化熔断（重启不丢失）
                if _cb is not None:
                    cb_open = await _cb.is_open()
                    if not cb_open:
                        await _cb.trip(
                            reason=trip_reason,
                            daily_loss=abs(daily_pnl)/equity,
                        )
                else:
                    global _circuit_broken, _circuit_reason
                    _circuit_broken = True
                    _circuit_reason = trip_reason
                logger.critical(f"💣 CIRCUIT BREAKER: {trip_reason}")
                await r.xadd(STREAM_SYSTEM_ALERTS, encode({
                    "type":      "circuit_breaker",
                    "reason":    trip_reason,
                    "daily_pnl": daily_pnl,
                }), maxlen=10000, approximate=True)

            # 发布风控状态（execution-service 强制 Gate 读这里）
            dd_status = _dd_controller.get_status()
            await r.set("cw4:risk:status", json.dumps({
                **dd_status,
                "new_trades_allowed": (not _circuit_broken) and dd_status["new_trades"],
                "circuit_broken":     _circuit_broken,
                "circuit_reason":     _circuit_reason,
                "daily_pnl":          _daily_pnl,
                "max_daily_loss_pct": MAX_DAILY_LOSS_PCT,
                "var_history_len":    len(_equity_history),
                "updated_at":         datetime.utcnow().isoformat(),
            }))

        except Exception as e:
            logger.error(f"portfolio_state_listener: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _cb
    r  = await get_redis()
    pg = await get_pg()

    # 初始化持久化熔断器
    _cb = PersistentCircuitBreaker(r, pg)
    # Redis 重启后从 PG 恢复状态
    restored = await _cb.restore_from_pg()
    if restored:
        logger.warning("熔断器状态已从 PostgreSQL 恢复 → OPEN")

    tasks = [
        asyncio.create_task(consume_loop()),
        asyncio.create_task(portfolio_state_listener()),
    ]
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="CryptoWatch v4 Risk Service", lifespan=lifespan)


@app.get("/health")
async def health():
    return {
        "status":  "ok",
        "service": "risk",
        "redis":   await redis_health(),
        "circuit_broken":   _circuit_broken,
        "drawdown_level":   _dd_controller.level.name,
        "new_trades":       _dd_controller.level.new_trades_allowed and not _circuit_broken,
        "daily_pnl":        _daily_pnl,
        "ts":               datetime.utcnow().isoformat(),
    }


@app.post("/circuit-breaker/reset")
async def reset_circuit():
    global _circuit_broken, _circuit_reason
    _circuit_broken = False
    _circuit_reason = ""
    _dd_controller.manual_reset()
    # 持久化解除
    if _cb is not None:
        await _cb.reset(reason="手动解除（API）")
    logger.warning("Circuit breaker MANUALLY RESET")
    return {"status": "reset", "ts": datetime.utcnow().isoformat()}


@app.get("/status")
async def status():
    return {
        **_dd_controller.get_status(),
        "circuit_broken":  _circuit_broken,
        "circuit_reason":  _circuit_reason,
        "daily_pnl":       _daily_pnl,
        "open_position_count": len(_open_positions) - 1,
        "equity_history_len":  len(_equity_history),
        "portfolio_risk":  await compute_portfolio_risk(),
    }


@app.get("/risk/portfolio-risk")
async def portfolio_risk():
    """Correlation-aware VaR + correlated-exposure + stress-scenario snapshot."""
    return await compute_portfolio_risk()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("SERVICE_PORT", 8004)))
