"""
services/signal/main.py  —  CryptoWatch v4 Signal Service

修复：
  ✅ 完整主循环（原来缺失）
  ✅ Regime → 策略切换（根据 regime 动态调整信号权重和过滤规则）
  ✅ IC/IR 在线计算（信息系数，验证 alpha 是否有效）
  ✅ 消费 market.candles + market.raw
  ✅ 产出 signal.scored 供 portfolio-service 消费
  ✅ 接入 onchain factor（get_onchain_factor）
"""

import asyncio
import json
import logging
import math
import os
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI

from services.signal.factors.composite import (
    FactorScores,
    MultiFactorScorer,
    get_onchain_factor,
    load_calibration,
    platt_fit,
    save_calibration,
    score_ema_alignment,
    score_funding_zscore,
    score_lsr_divergence,
    score_oi_momentum,
    score_rsi,
)
from services.signal.ic_health import ic_health_scalar
from services.signal.regime.detector import RegimeDetector

# ── 新增：HMM Regime + EWMA权重学习 ──────────────────────
from services.signal.regime.hmm_detector import (
    HMMRegimeDetector,
    fuse_regimes,
    get_hmm_regime,
)
from services.signal.weight_learner import (
    WeightLearner,
    get_learner_status,
    read_dynamic_weights,
)
from shared.db.connections import get_pg, get_redis, redis_health
from shared.events.streams import (
    CG_SIGNAL,
    STREAM_MARKET_CANDLES,
    STREAM_MARKET_RAW,
    STREAM_ORDER_LIFECYCLE,
    STREAM_SIGNAL_SCORED,
    decode,
    encode,
    run_forever,
)
from shared.models.signal import (
    Direction,
    Regime,
    ScoredSignal,
    SignalType,
)

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

CG = CG_SIGNAL
MIN_WIN_PROB = float(os.getenv("MIN_WIN_PROBABILITY", "0.55"))
MIN_DATA_QUAL = float(os.getenv("MIN_DATA_QUALITY", "0.75"))
COOLDOWN_SECS = int(os.getenv("SIGNAL_COOLDOWN_SECS", "3600"))
# 过期 K 线守卫:bar 收盘后超过该秒数才被消费(断连恢复的积压回放),只热身
# detector/缓存,不评分——语义同 backfill(2026-07-12 事故积压 1.2 万根 K 线,
# 逐根回放会拿几天前的价格发"当前"信号)。scanner 固定 1h bar。
CANDLE_INTERVAL_SECS = int(os.getenv("CANDLE_INTERVAL_SECS", "3600"))
STALE_CANDLE_SECS = int(os.getenv("STALE_CANDLE_SECS", "900"))
# candles 表 interval 标签,须与 scanner 的 CANDLE_INTERVAL 及 HMM/risk 查询一致
CANDLE_INTERVAL_LABEL = os.getenv("CANDLE_INTERVAL", "1h")

# ── Regime → 策略开关（核心修复：Regime 驱动策略切换）──────────────────────────
#
# 每个 regime 定义：
#   allowed_signals: 允许的信号类型
#   weight_overrides: 覆盖 WEIGHTS 中的特定因子权重（sum 不变，内部归一化）
#   min_win_prob: 该 regime 下的最低胜率门槛
#   kelly_fraction: Kelly 系数
#
REGIME_STRATEGY = {
    Regime.TRENDING_UP: {
        "allowed": {SignalType.TREND_CONFIRM, SignalType.LONG_SETUP},
        "blocked": {SignalType.SHORT_SETUP, SignalType.BEAR_SQUEEZE},
        "weight_adj": {"technical_ema": 1.5, "oi_momentum": 1.3},  # 趋势因子加权
        "min_win_prob": 0.54,
        "kelly_fraction": 0.6,
        "max_hold_h": 48,
    },
    Regime.TRENDING_DOWN: {
        "allowed": {SignalType.SHORT_SETUP, SignalType.BEAR_SQUEEZE},
        "blocked": {SignalType.LONG_SETUP, SignalType.TREND_CONFIRM},
        "weight_adj": {"technical_ema": 1.5, "funding_zscore": 1.2},
        "min_win_prob": 0.54,
        "kelly_fraction": 0.6,
        "max_hold_h": 48,
    },
    Regime.RANGING: {
        "allowed": {
            SignalType.LONG_SETUP,
            SignalType.SHORT_SETUP,
            SignalType.FR_ARB,
            SignalType.CROWD_LONG,
            SignalType.CROWD_SHORT,
        },
        "blocked": {SignalType.TREND_CONFIRM},
        "weight_adj": {
            "funding_zscore": 1.4,
            "lsr_divergence": 1.3,
        },  # 均值回归因子加权
        "min_win_prob": 0.55,
        "kelly_fraction": 0.4,
        "max_hold_h": 24,
    },
    Regime.HIGH_VOLATILITY: {
        "allowed": {SignalType.FR_ARB},  # 高波动只做资金费套利
        "blocked": {
            SignalType.LONG_SETUP,
            SignalType.SHORT_SETUP,
            SignalType.TREND_CONFIRM,
            SignalType.BEAR_SQUEEZE,
        },
        "weight_adj": {},
        "min_win_prob": 0.55,
        "kelly_fraction": 0.3,
        "max_hold_h": 8,
    },
    Regime.ACCUMULATION: {
        "allowed": {
            SignalType.LONG_SETUP,
            SignalType.TREND_CONFIRM,
            SignalType.CROWD_SHORT,
        },  # 逆势看多
        "blocked": {SignalType.SHORT_SETUP},
        "weight_adj": {"onchain": 1.6, "oi_momentum": 1.2},  # 链上数据更重要
        "min_win_prob": 0.55,
        "kelly_fraction": 0.5,
        "max_hold_h": 72,
    },
    Regime.UNKNOWN: {
        "allowed": {SignalType.LONG_SETUP, SignalType.SHORT_SETUP, SignalType.FR_ARB},
        "blocked": {
            SignalType.TREND_CONFIRM,
            SignalType.BEAR_SQUEEZE,
            SignalType.CROWD_LONG,
            SignalType.CROWD_SHORT,
        },
        "weight_adj": {},
        "min_win_prob": 0.55,
        "kelly_fraction": 0.3,
        "max_hold_h": 12,
    },
}


# ── 在线 IC/IR 计算（alpha 验证）──────────────────────────────────────────────


class ICTracker:
    """
    Information Coefficient (IC) 在线计算。
    IC = Spearman 相关系数（预测得分 vs 实际收益）
    IR = IC 均值 / IC 标准差（越高越稳定）

    每个信号触发后记录预测分，N 小时后回填实际收益，计算 IC。
    """

    def __init__(self, symbol: str, window: int = 100):
        self.symbol = symbol
        self._window = window
        self._records: deque = deque(maxlen=window)  # (score, actual_ret, ts)
        self._pending: dict = {}  # signal_id → (score, price_at_signal, ts)

    def record_signal(self, signal_id: str, score: float, price: float):
        self._pending[signal_id] = {"score": score, "price": price, "ts": time.time()}

    def record_outcome(self, signal_id: str, exit_price: float):
        pending = self._pending.pop(signal_id, None)
        if not pending or pending["price"] <= 0:
            return
        ret = (exit_price - pending["price"]) / pending["price"]
        self._records.append((pending["score"], ret, pending["ts"]))

    def calc_ic(self) -> dict:
        if len(self._records) < 10:
            return {"ic": None, "ir": None, "n": len(self._records)}

        scores = [r[0] for r in self._records]
        returns = [r[1] for r in self._records]

        # Spearman rank correlation (tie-correct — see _spearman)
        n = len(scores)
        ic = _spearman(scores, returns)

        # IR = IC / IC_std (rolling)
        recent = [self._calc_single_ic(i) for i in range(min(20, n))]
        ir = None
        if len(recent) >= 5:
            mean_ic = sum(recent) / len(recent)
            std_ic = math.sqrt(sum((x - mean_ic) ** 2 for x in recent) / len(recent))
            ir = mean_ic / std_ic if std_ic > 0 else 0.0

        return {
            "symbol": self.symbol,
            "ic": round(ic, 4),
            "ir": round(ir, 4) if ir is not None else None,
            "n": n,
            "mean_return": round(sum(returns) / n, 6),
        }

    def _calc_single_ic(self, idx: int) -> float:
        """计算最近 idx 条记录的 IC"""
        sample = list(self._records)[max(0, -10 - idx) : -idx if idx else None]
        if len(sample) < 5:
            return 0.0
        sc = [r[0] for r in sample]
        re = [r[1] for r in sample]
        return _spearman(sc, re)


def _rank(values: list) -> list:
    # Average ranks (ties share the mean rank) — required for a correct Spearman under ties.
    from scipy.stats import rankdata

    return rankdata(values, method="average").tolist()


def _spearman(a: list, b: list) -> float:
    """Spearman ρ = Pearson correlation of average ranks. Correct under ties, unlike the
    1−6Σd²/(n(n²−1)) shortcut which assumes all ranks are distinct (P2-4): tied scores or
    returns (common with discretised signals / flat bars) biased the old IC."""
    ra, rb = _rank(a), _rank(b)
    n = len(ra)
    if n == 0:
        return 0.0
    ma = sum(ra) / n
    mb = sum(rb) / n
    cov = sum((ra[i] - ma) * (rb[i] - mb) for i in range(n))
    va = sum((x - ma) ** 2 for x in ra)
    vb = sum((x - mb) ** 2 for x in rb)
    if va <= 0 or vb <= 0:
        return 0.0
    return cov / (math.sqrt(va) * math.sqrt(vb))


def _to_float(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


# ── Signal Service 核心 ────────────────────────────────────────────────────────


class SignalService:
    def __init__(self):
        # 每个 symbol 一个 RegimeDetector（ADX+ATR 快信号）
        self._detectors: dict[str, RegimeDetector] = {}
        self._scorer = MultiFactorScorer()
        # 每个 symbol 的市场数据缓存
        self._market: dict[str, dict] = {}
        # 冷却（防止同一 symbol 频繁触发）
        self._cooldowns: dict[str, float] = {}
        # IC 追踪
        self._ic_trackers: dict[str, ICTracker] = {}
        # onchain factor 缓存
        self._onchain_cache: dict[str, float] = {}
        # HMM Regime 缓存（慢信号，每6小时更新）
        self._hmm_cache: dict[str, dict] = {}
        # EWMA 动态权重缓存（每小时更新）
        self._dyn_weights: dict[str, float] = {}
        # redis 客户端引用（_score_and_emit 里读 HMM / 动态权重）
        self._redis = None

    def _get_detector(self, symbol: str) -> RegimeDetector:
        if symbol not in self._detectors:
            self._detectors[symbol] = RegimeDetector(symbol)
        return self._detectors[symbol]

    def _get_ic(self, symbol: str) -> ICTracker:
        if symbol not in self._ic_trackers:
            self._ic_trackers[symbol] = ICTracker(symbol)
        return self._ic_trackers[symbol]

    def record_signal_outcome(self, signal_id: str, exit_price: float) -> bool:
        """Backfill a closed trade's realized exit into the IC tracker that holds
        the originating signal (item #4 — closes the IC-decay feedback loop).

        Dispatches to every tracker; record_outcome is a no-op on trackers that
        don't hold the signal_id, so we don't need to know the symbol format used
        by the execution service. Returns True if a tracker recorded it."""
        if not signal_id or exit_price is None or exit_price <= 0:
            return False
        recorded = False
        for tracker in self._ic_trackers.values():
            if signal_id in tracker._pending:
                tracker.record_outcome(signal_id, exit_price)
                recorded = True
        return recorded

    # ── 处理 K 线数据 ──────────────────────────────────
    async def _persist_candle(self, symbol: str, data: dict, open_time_ms: float):
        """写 TimescaleDB candles 表(HMM lookback 与 risk 相关性查询的数据源)。
        open/volume 缺失(旧格式消息)或 PG 故障时跳过——持久化失败不阻塞评分。"""
        open_px = data.get("open")
        volume = data.get("volume")
        if not open_time_ms or open_px is None or volume is None:
            return
        try:
            pool = await get_pg()
            await pool.execute(
                """
                INSERT INTO candles (time, symbol, interval, open, high, low, close, volume)
                VALUES (to_timestamp($1), $2, $3, $4, $5, $6, $7, $8)
                ON CONFLICT (symbol, interval, time) DO NOTHING
                """,
                open_time_ms / 1000,
                symbol,
                CANDLE_INTERVAL_LABEL,
                float(open_px),
                float(data.get("high", 0)),
                float(data.get("low", 0)),
                float(data.get("close", 0)),
                float(volume),
            )
        except Exception as e:
            logger.warning(f"candle persist {symbol}: {e}")

    async def handle_candle(self, data: dict):
        symbol = data.get("symbol", "")
        if not symbol:
            return

        high = float(data.get("high", 0))
        low = float(data.get("low", 0))
        close = float(data.get("close", 0))

        if not (high and low and close):
            return

        # ADX+ATR 快信号 Regime
        detector = self._get_detector(symbol)
        adxatr_regime = detector.update(high, low, close)

        # HMM 慢信号 Regime（从缓存读，异步刷新）
        hmm_data = self._hmm_cache.get(symbol, {"state": "range", "confidence": 0.0})
        hmm_state = hmm_data.get("state", "range")
        hmm_conf = float(hmm_data.get("confidence", 0.0))

        # 融合两个 Regime → 最终 Regime + Kelly 系数
        fused_regime, kelly_scalar = fuse_regimes(
            hmm_state=hmm_state,
            hmm_conf=hmm_conf,
            adxatr_regime=adxatr_regime,
            adxatr_bars=detector._regime_bars,
        )

        # 更新市场数据缓存
        mkt = self._market.setdefault(symbol, {})
        mkt["price"] = close
        mkt["high"] = high
        mkt["low"] = low
        mkt["regime"] = adxatr_regime  # 原始 Regime 保留
        mkt["fused_regime"] = fused_regime
        mkt["kelly_scalar"] = kelly_scalar
        mkt["hmm_state"] = hmm_state
        mkt["indicators"] = data.get("indicators", {})
        open_time_ms = float(data.get("open_time") or 0)
        if open_time_ms:
            mkt["bar_close_ts"] = open_time_ms / 1000 + CANDLE_INTERVAL_SECS

        # 落库 TimescaleDB candles(此前无生产者,HMM/risk 相关性查询永远空手):
        # backfill 种子也写——一次播种即给 HMM 250 根历史。唯一索引去重,失败不影响评分。
        await self._persist_candle(symbol, data, open_time_ms)

        # 历史回填(market-scanner 给新发现的符号播种 regime 缓冲):只热身
        # detector/缓存,不评分——对着几天前的价格发"当前"信号是错的,而且一次
        # 中途发射就会占掉 1h 冷却窗,把种子完成后的首次真实评分挡住。
        if data.get("backfill"):
            return

        # 过期 K 线(消费积压回放):同 backfill,只热身不评分
        if open_time_ms and time.time() - mkt["bar_close_ts"] > STALE_CANDLE_SECS:
            return

        # 异步刷新 HMM 缓存
        asyncio.create_task(self._refresh_hmm(symbol))

        # 触发信号评分（传入融合后的 Regime 字符串）
        await self._score_and_emit(symbol, adxatr_regime, fused_regime, kelly_scalar)

    # ── 处理实时市场快照（funding / OI / LSR）──────────
    async def handle_market_raw(self, data: dict):
        symbol = data.get("symbol", "")
        if not symbol:
            return
        mkt = self._market.setdefault(symbol, {})
        mkt.update(
            {
                "funding_rate": float(data.get("funding_rate", 0)),
                "oi_change_pct": float(data.get("oi_change_pct", 0)),
                "price_change_pct": float(data.get("price_change_pct", 0)),
                "long_ratio": float(data.get("long_ratio", 50)),
                "funding_history": data.get("funding_history", []) or [],
            }
        )
        # Raw snapshot often arrives after seeded candles on startup.
        # Re-score once real-time funding/OI/LSR data is available so we
        # don't wait until the next hourly candle to produce actionable signals.
        regime = mkt.get("regime")
        if regime is not None and mkt.get("price"):
            await self._score_and_emit(
                symbol,
                regime,
                mkt.get("fused_regime", ""),
                float(mkt.get("kelly_scalar", 1.0)),
            )

    # ── 核心评分 ──────────────────────────────────────
    async def _score_and_emit(
        self,
        symbol: str,
        regime: Regime,
        fused_regime: str = "",
        kelly_scalar: float = 1.0,
    ):
        mkt = self._market.get(symbol, {})
        price = mkt.get("price", 0)
        if not price:
            return

        # 价格时效:candle 断供时 market.raw 仍会触发评分,不能拿过期收盘价当现价
        bar_close_ts = mkt.get("bar_close_ts")
        if bar_close_ts and time.time() - bar_close_ts > CANDLE_INTERVAL_SECS + STALE_CANDLE_SECS:
            return

        # 冷却检查
        if time.time() < self._cooldowns.get(symbol, 0):
            return

        # 策略开关：根据 ADX+ATR regime 确定可用信号类型
        strategy = REGIME_STRATEGY.get(regime, REGIME_STRATEGY[Regime.UNKNOWN])
        allowed = strategy["allowed"]
        min_prob = strategy["min_win_prob"]
        max_hold = strategy["max_hold_h"]

        # Kelly 系数 = 策略基础值 × HMM融合系数
        # crisis → kelly_scalar=0.1，trend_confirmed → kelly_scalar=1.3
        base_kelly = strategy["kelly_fraction"]
        kelly_f = round(base_kelly * kelly_scalar, 4)

        # HMM crisis 状态：缩减 kelly，但仍允许方向性交易
        if fused_regime == "crisis":
            allowed = {SignalType.LONG_SETUP, SignalType.SHORT_SETUP, SignalType.FR_ARB}
            kelly_f = round(base_kelly * 0.3, 4)

        if not allowed:
            return

        # 从 onchain-sentinel 读取链上因子（异步，降级为 0.0）
        onchain = self._onchain_cache.get(symbol, 0.0)
        asyncio.create_task(self._refresh_onchain(symbol))

        # 组装 FactorScores
        indicators = mkt.get("indicators", {})
        factors = FactorScores(
            technical_rsi=score_rsi(indicators.get("rsi")),
            technical_ema=score_ema_alignment(
                indicators.get("ema20"),
                indicators.get("ema50"),
                indicators.get("ema200"),
                price,
            ),
            funding_zscore=score_funding_zscore(
                mkt.get("funding_rate", 0),
                indicators.get("funding_history") or mkt.get("funding_history", []),
            ),
            oi_momentum=score_oi_momentum(
                mkt.get("oi_change_pct"),
                mkt.get("price_change_pct"),
            ),
            lsr_divergence=score_lsr_divergence(mkt.get("long_ratio", 50)),
            onchain=onchain,
            social=0.0,
            orderbook=0.0,
        )

        # 应用 regime 权重调整（ADX+ATR 层）
        weight_adj = strategy.get("weight_adj", {})
        if weight_adj:
            factors = self._apply_weight_adj(factors, weight_adj)

        # 应用 EWMA 动态权重（覆盖静态权重）
        dyn_weights = self._dyn_weights
        if dyn_weights:
            factors = self._apply_dynamic_weights(factors, dyn_weights)

        # Wilson CI 用该 symbol 当前已观察的信号数
        ic_tracker_early = self._get_ic(symbol)
        self._scorer.set_sample_size(max(10, len(ic_tracker_early._records)))

        # ── IC-decay closed loop ── throttle sizing as realized alpha (rolling IC) fades,
        # restore it as IC recovers. Neutral until enough outcomes have accumulated.
        _ic_stats = ic_tracker_early.calc_ic()
        ic_scalar = ic_health_scalar(_ic_stats.get("ic"), _ic_stats.get("n", 0))
        if ic_scalar < 1.0:
            kelly_f = round(kelly_f * ic_scalar, 4)
            logger.info(
                f"IC-decay throttle {symbol}: ic={_ic_stats.get('ic')} n={_ic_stats.get('n')} kelly×{ic_scalar}"
            )

        # 判断方向（基于综合得分）
        long_score = self._scorer.score("LONG", factors)
        short_score = self._scorer.score("SHORT", factors)

        # 选最优方向
        if long_score.win_probability >= short_score.win_probability:
            best_score = long_score
            direction = Direction.LONG
            sig_type = SignalType.LONG_SETUP
        else:
            best_score = short_score
            direction = Direction.SHORT
            sig_type = SignalType.SHORT_SETUP

        # 策略过滤（regime 不允许该信号类型，直接跳过）
        if sig_type in strategy.get("blocked", set()):
            return
        if sig_type not in allowed:
            return

        # 胜率门槛
        if best_score.win_probability < min_prob:
            return

        # 数据质量检查：基于可用因子比例 + 指标完整性
        data_quality = self._compute_data_quality(factors, indicators, mkt)
        if data_quality < MIN_DATA_QUAL:
            return

        # 构建 ScoredSignal
        detector = self._get_detector(symbol)
        atr = detector.params.get("atr_mult", 2.0) * (mkt.get("high", price) - mkt.get("low", price))
        stop_loss = price - atr if direction == Direction.LONG else price + atr
        take_profit = price + atr * 1.5 if direction == Direction.LONG else price - atr * 1.5

        signal = ScoredSignal(
            symbol=symbol,
            signal_type=sig_type,
            direction=direction,
            regime=regime,
            win_probability=best_score.win_probability,
            confidence_lo=best_score.confidence_lo,
            confidence_hi=best_score.confidence_hi,
            expected_return=best_score.direction_score * 0.05,
            factor_scores=best_score.factor_scores,
            regime_adjusted=bool(weight_adj),
            entry_price=price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            max_hold_hours=max_hold,
            data_quality=data_quality,
            indicators=indicators,
        )

        # IC 记录（复用前面取过的 tracker）
        ic_tracker = ic_tracker_early
        ic_tracker.record_signal(signal.id, best_score.raw, price)

        # 发布到 signal.scored
        r = await get_redis()
        signal_dict = signal.to_dict()
        signal_dict["kelly_fraction"] = kelly_f
        signal_dict["ic_scalar"] = ic_scalar  # IC-decay throttle applied to sizing
        # 保留 raw 评分用于后续 calibration refit
        signal_dict["raw"] = best_score.raw
        await r.xadd(STREAM_SIGNAL_SCORED, encode(signal_dict), maxlen=10000, approximate=True)

        # 缓存到 Redis（供 gateway 展示）
        await r.hset("cw4:signals:recent", signal.id, json.dumps(signal_dict))
        # 更新 regime 缓存
        await r.hset("cw4:regimes", symbol, json.dumps(detector.get_status()))

        # 持久化到 TimescaleDB
        try:
            import uuid as _uuid

            from shared.db.connections import get_pg

            pg = await get_pg()
            async with pg.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO signals
                        (id, symbol, regime, signal_type, direction,
                         win_probability, confidence_lo, confidence_hi,
                         expected_return, factor_scores, regime_adjusted,
                         entry_price, stop_loss, take_profit, max_hold_hours,
                         data_quality, status)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,'pending')
                    ON CONFLICT (id) DO NOTHING
                """,
                    _uuid.UUID(signal.id),
                    signal.symbol,
                    signal.regime.value,
                    signal.signal_type.value,
                    signal.direction.value,
                    signal.win_probability,
                    signal.confidence_lo,
                    signal.confidence_hi,
                    signal.expected_return,
                    json.dumps(signal.factor_scores) if signal.factor_scores else None,
                    signal.regime_adjusted,
                    signal.entry_price,
                    signal.stop_loss,
                    signal.take_profit,
                    signal.max_hold_hours,
                    signal.data_quality,
                )
        except Exception as _e:
            logger.warning(f"signal DB persist failed: {_e}")

        # 设置冷却
        self._cooldowns[symbol] = time.time() + COOLDOWN_SECS

        logger.info(
            f"SIGNAL {sig_type.value} {symbol} {direction.value} "
            f"p={signal.win_probability:.0%} "
            f"regime={regime.value} "
            f"onchain={onchain:+.3f}"
        )

        # IC 统计定期写 Redis
        ic = ic_tracker.calc_ic()
        if ic["n"] >= 10:
            await r.hset("cw4:ic_stats", symbol, json.dumps(ic))

    def _compute_data_quality(self, factors: "FactorScores", indicators: dict, mkt: dict) -> float:
        """
        数据质量评分（0~1）：
          - 50% 来自核心指标完整性（RSI / EMA20 / EMA50 / EMA200 / funding_history）
          - 30% 来自因子非零比例（至少几个因子真的在驱动评分）
          - 20% 来自实时市场数据新鲜度（funding / OI / LSR 是否存在）
        """
        # 1) 核心指标
        core_keys = ["rsi", "ema20", "ema50", "ema200"]
        core_present = sum(1 for k in core_keys if indicators.get(k) is not None)
        fh = indicators.get("funding_history") or mkt.get("funding_history") or []
        fh_bonus = 1.0 if len(fh) >= 20 else (len(fh) / 20.0)
        indicator_score = (core_present / len(core_keys)) * 0.7 + fh_bonus * 0.3

        # 2) 因子非零比例（排除未实现的因子）
        import dataclasses

        _UNIMPLEMENTED = {"social", "orderbook"}
        d = {k: v for k, v in dataclasses.asdict(factors).items() if k not in _UNIMPLEMENTED}
        nonzero = sum(1 for v in d.values() if abs(float(v)) > 1e-6)
        factor_score = nonzero / len(d) if d else 0.0

        # 3) 市场数据新鲜度
        freshness = 0.0
        freshness += 0.4 if mkt.get("funding_rate") is not None else 0.0
        freshness += 0.3 if mkt.get("long_ratio") else 0.0
        freshness += 0.3 if mkt.get("price_change_pct") is not None else 0.0

        q = 0.5 * indicator_score + 0.3 * factor_score + 0.2 * freshness
        return round(max(0.0, min(1.0, q)), 4)

    def _apply_weight_adj(self, factors: FactorScores, adj: dict) -> FactorScores:
        """按 regime 调整因子权重（通过缩放对应因子值实现）"""
        import dataclasses

        d = dataclasses.asdict(factors)
        for factor, multiplier in adj.items():
            if factor in d:
                d[factor] = max(-1.0, min(1.0, d[factor] * multiplier))
        return FactorScores(**d)

    async def _refresh_onchain(self, symbol: str):
        """后台异步刷新 onchain factor（不阻塞评分循环）"""
        try:
            score = await get_onchain_factor(symbol)
            self._onchain_cache[symbol] = score
        except Exception:
            pass

    async def _refresh_hmm(self, symbol: str):
        """后台异步刷新 HMM Regime 缓存（不阻塞评分循环）"""
        try:
            if self._redis:
                data = await get_hmm_regime(self._redis, symbol)
                self._hmm_cache[symbol] = data
        except Exception:
            pass

    async def _refresh_dynamic_weights(self):
        """后台异步刷新 EWMA 动态权重（不阻塞评分循环）"""
        try:
            if self._redis:
                self._dyn_weights = await read_dynamic_weights(self._redis)
        except Exception:
            pass

    def _apply_dynamic_weights(self, factors: FactorScores, weights: dict[str, float]) -> FactorScores:
        """
        用 EWMA 学习到的动态权重调节各因子值。
        动态权重影响的是因子的相对重要性（通过缩放因子值实现）。
        weights 格式：{factor_name: dynamic_weight}
        比例：dyn_w / base_w 表示相对于基础权重的倍数。
        """
        import dataclasses

        from services.signal.weight_learner import BASE_WEIGHTS

        d = dataclasses.asdict(factors)
        for factor, dyn_w in weights.items():
            if factor not in d:
                continue
            base_w = BASE_WEIGHTS.get(factor, 0.1)
            if base_w <= 0:
                continue
            ratio = dyn_w / base_w  # 0.5~1.5 之间
            d[factor] = max(-1.0, min(1.0, d[factor] * ratio))
        return FactorScores(**d)


# ── 主服务 ────────────────────────────────────────────

_svc = SignalService()


async def calibration_refit_loop():
    """
    每 6 小时从 PG 拉近 30 天成交 (score, pnl) 对，拟合 Platt calibration。
    样本不足时跳过，保持既有参数。
    """
    from datetime import datetime, timedelta, timezone

    from shared.db.connections import get_pg

    r = await get_redis()
    pg = await get_pg()

    while True:
        try:
            since = datetime.now(timezone.utc) - timedelta(days=30)
            async with pg.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT
                        (a.payload->>'raw')::float     AS raw,
                        o.realized_pnl
                    FROM audit_log a
                    JOIN orders o ON (a.payload->>'signal_id') = o.signal_id::text
                    WHERE a.event_type = 'ORDER_FILLED'
                      AND a.time >= $1
                      AND o.realized_pnl IS NOT NULL
                      AND a.payload ? 'raw'
                """,
                    since,
                )
            scores = [float(row["raw"]) for row in rows if row["raw"] is not None]
            outcomes = [1 if float(row["realized_pnl"] or 0) > 0 else 0 for row in rows if row["raw"] is not None]
            if len(scores) >= 30:
                c, s = platt_fit(scores, outcomes)
                _svc._scorer.update_calibration(c, s)
                await save_calibration(r, c, s, len(scores))
                logger.info(f"Calibration refit: center={c} scale={s} n={len(scores)}")
            else:
                logger.debug(f"calibration_refit: 样本不足 ({len(scores)}/30)，跳过")
        except Exception as e:
            logger.warning(f"calibration_refit: {e}")

        await asyncio.sleep(6 * 3600)


async def consume_loop():
    r = await get_redis()

    for stream in [STREAM_MARKET_CANDLES, STREAM_MARKET_RAW, STREAM_ORDER_LIFECYCLE]:
        try:
            await r.xgroup_create(stream, CG, id="0", mkstream=True)
        except Exception as e:
            if "BUSYGROUP" not in str(e):
                raise

    logger.info("Signal service ready")

    while True:
        candle_results = await r.xreadgroup(
            CG,
            "signal-candles",
            {STREAM_MARKET_CANDLES: ">"},
            count=20,
            block=500,
        )
        for _, messages in candle_results or []:
            for msg_id, fields in messages:
                try:
                    await _svc.handle_candle(decode(fields))
                    await r.xack(STREAM_MARKET_CANDLES, CG, msg_id)
                except Exception as e:
                    logger.error(f"candle: {e}", exc_info=True)

        raw_results = await r.xreadgroup(
            CG,
            "signal-raw",
            {STREAM_MARKET_RAW: ">"},
            count=20,
            block=500,
        )
        for _, messages in raw_results or []:
            for msg_id, fields in messages:
                try:
                    await _svc.handle_market_raw(decode(fields))
                    await r.xack(STREAM_MARKET_RAW, CG, msg_id)
                except Exception as e:
                    logger.error(f"market_raw: {e}", exc_info=True)

        # Order lifecycle → close the IC loop (item #4): on a CLOSED position,
        # backfill the realized exit price into the originating signal's IC tracker.
        lifecycle_results = await r.xreadgroup(
            CG,
            "signal-lifecycle",
            {STREAM_ORDER_LIFECYCLE: ">"},
            count=50,
            block=500,
        )
        for _, messages in lifecycle_results or []:
            for msg_id, fields in messages:
                try:
                    ev = decode(fields)
                    if ev.get("event") == "closed" or ev.get("state") == "CLOSED":
                        _svc.record_signal_outcome(ev.get("signal_id", ""), _to_float(ev.get("exit_price")))
                    await r.xack(STREAM_ORDER_LIFECYCLE, CG, msg_id)
                except Exception as e:
                    logger.error(f"lifecycle: {e}", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from shared.db.connections import get_pg

    r = await get_redis()
    pg = await get_pg()

    # 把 redis 注入到 SignalService 实例
    _svc._redis = r

    # 启动时从 Redis 加载 calibration（若已有）
    cal = await load_calibration(r)
    if cal:
        center, scale = cal
        _svc._scorer.update_calibration(center, scale)
        logger.info(f"Loaded persisted calibration center={center} scale={scale}")

    # 启动 HMM Regime 检测器（后台协程，每6小时重训练）
    symbols = os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT").split(",")
    hmm_detector = HMMRegimeDetector(r, pg, symbols)
    hmm_task = asyncio.create_task(run_forever("hmm_detector", hmm_detector.run))

    # 启动 EWMA 权重学习器（后台协程，每小时更新）
    weight_learner = WeightLearner(r, pg)
    wl_task = asyncio.create_task(run_forever("weight_learner", weight_learner.run))

    # onchain→signal 桥：消费 signal.raw 里 onchain-sentinel 的鲸鱼流更新，收到即刷新
    # 该 symbol 的链上因子缓存。否则 signal.raw 是只产不消的孤儿流。(audit P1-a)
    from services.signal.factors.composite import get_onchain_factor
    from services.signal.onchain.bridge import consume_onchain_factor_updates

    async def _onchain_rescore(symbol: str):
        _svc._onchain_cache[symbol] = await get_onchain_factor(symbol)

    onchain_bridge_task = asyncio.create_task(
        run_forever("onchain_bridge", lambda: consume_onchain_factor_updates(_onchain_rescore))
    )

    # 主消费循环（run_forever:Redis 断连不再永久杀死消费任务,2026-07-12 事故）
    consume_task = asyncio.create_task(run_forever("consume_loop", consume_loop))

    # Calibration 定期 refit（每6小时）
    cal_task = asyncio.create_task(run_forever("calibration_refit", calibration_refit_loop))

    logger.info(
        f"Signal service ready | HMM symbols={symbols} | WeightLearner ON | CalibrationRefit ON | OnchainBridge ON"
    )
    yield

    for t in [consume_task, hmm_task, wl_task, cal_task, onchain_bridge_task]:
        t.cancel()
    hmm_detector.stop()
    weight_learner.stop()


app = FastAPI(title="CryptoWatch v4 Signal Service", lifespan=lifespan)


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
            "labels": {"service": "signal"},
            "help": "service process is up",
        }
    ]
    try:
        from shared.db.connections import redis_health

        out.append(
            {
                "name": "selene_redis_up",
                "value": await redis_health(),
                "labels": {"service": "signal"},
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
        "service": "signal",
        "redis": await redis_health(),
        "symbols_tracked": len(_svc._detectors),
        "ts": datetime.utcnow().isoformat(),
    }


@app.get("/regime/{symbol}")
async def regime(symbol: str):
    det = _svc._detectors.get(symbol.upper())
    if not det:
        return {"error": "not_tracked"}
    return det.get_status()


@app.get("/ic/{symbol}")
async def ic_stats(symbol: str):
    tracker = _svc._ic_trackers.get(symbol.upper())
    if not tracker:
        return {"error": "no_data"}
    return tracker.calc_ic()


@app.get("/regime/hmm/{symbol}")
async def hmm_regime(symbol: str):
    """HMM 慢信号 Regime 状态"""
    if _svc._redis:
        return await get_hmm_regime(_svc._redis, symbol.upper())
    return {"error": "redis_not_ready"}


@app.get("/regime/fused/{symbol}")
async def fused_regime(symbol: str):
    """HMM + ADX+ATR 融合后的最终 Regime 状态"""
    sym = symbol.upper()
    mkt = _svc._market.get(sym, {})
    return {
        "symbol": sym,
        "fused_regime": mkt.get("fused_regime", "unknown"),
        "kelly_scalar": mkt.get("kelly_scalar", 1.0),
        "hmm_state": mkt.get("hmm_state", "range"),
        "adxatr_regime": str(mkt.get("regime", "UNKNOWN")),
    }


@app.get("/weights")
async def dynamic_weights():
    """EWMA 学习到的当前动态因子权重"""
    if _svc._redis:
        return await get_learner_status(_svc._redis)
    return {"error": "redis_not_ready"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("SERVICE_PORT", 8002)))
