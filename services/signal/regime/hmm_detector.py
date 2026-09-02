"""
services/signal/regime/hmm_detector.py

移植自 quant-stack/services/regime-detector/src/detector.py
适配 cryptowatch v4，作为异步协程运行（不是独立服务）

HMM 特征（与 quant-stack 保持一致）：
  f0: realized_vol   — 20期收益率标准差（实现波动率）
  f1: price_autocorr — 5期收益率 lag-1 自相关系数
  f2: fear_greed     — 恐贪指数归一化 [0,1]（无数据时用 0.5）

3个隐状态，按特征均值自动打标签：
  最高 realized_vol → crisis
  其余按 autocorr 排：高→trend，低→range

Redis Key：
  cw4:regime:hmm:{symbol}  → {state, confidence, updated_at, features}

与 ADX+ATR（RegimeDetector）的关系：
  HMM 每6小时重训练，提供中期市场结构判断（慢信号）
  ADX+ATR 每根K线更新，提供短期技术确认（快信号）
  两者融合在 fuse_regimes() 里
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import numpy as np

logger = logging.getLogger("signal.hmm_detector")

REFRESH_HOURS = int(os.getenv("HMM_REFRESH_HOURS", "6"))
LOOKBACK_BARS = int(os.getenv("HMM_LOOKBACK_BARS", "800"))
N_COMPONENTS = 3
N_ITER = 200
MIN_ROWS = 60  # 低于此值冷启动
REDIS_TTL = 3600 * 8  # 8小时

REDIS_KEY_PREFIX = "cw4:regime:hmm"


class HMMRegimeDetector:
    """
    异步 HMM Regime 检测器。
    在 signal-service 的 lifespan 中 create_task 启动。
    每 REFRESH_HOURS 小时重训练，结果写入 Redis。
    """

    def __init__(self, redis_client, pg_pool, symbols: list[str]):
        self._r = redis_client
        self._pg = pg_pool
        self._symbols = symbols
        self._running = False

    async def run(self):
        self._running = True
        logger.info(
            f"HMMRegimeDetector 启动 symbols={self._symbols} refresh={REFRESH_HOURS}h lookback={LOOKBACK_BARS}bars"
        )

        # 启动时立即跑一次
        await self._refresh_all()

        while self._running:
            await asyncio.sleep(REFRESH_HOURS * 3600)
            try:
                await self._refresh_all()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"HMM refresh error: {e}", exc_info=True)

    def stop(self):
        self._running = False

    # ── 全量更新 ──────────────────────────────────────────
    async def _refresh_all(self):
        for symbol in self._symbols:
            try:
                await self._refresh_symbol(symbol)
            except Exception as e:
                logger.warning(f"HMM {symbol} error: {e}")

    async def _refresh_symbol(self, symbol: str):
        # 从 TimescaleDB 加载收盘价
        closes = await self._load_closes(symbol)
        if closes is None or len(closes) < MIN_ROWS:
            logger.warning(
                f"HMM {symbol}: 数据不足({len(closes) if closes is not None else 0}/{MIN_ROWS})，冷启动写 range"
            )
            await self._write_result(symbol, "range", 0.0, {})
            return

        # 恐贪指数
        fear_greed = await self._load_fear_greed()

        # 计算特征矩阵
        features = self._build_features(closes, fear_greed)
        if features is None or len(features) < MIN_ROWS:
            await self._write_result(symbol, "range", 0.0, {})
            return

        # HMM 训练（CPU 密集，放到线程池）
        state, confidence, feat_summary = await asyncio.to_thread(self._fit_and_predict, features)

        await self._write_result(symbol, state, confidence, feat_summary)
        logger.info(f"HMM {symbol}: state={state} confidence={confidence:.2f} features={feat_summary}")

    # ── 特征工程 ──────────────────────────────────────────
    def _build_features(self, closes: np.ndarray, fear_greed: float) -> Optional[np.ndarray]:
        try:
            returns = np.diff(np.log(closes + 1e-10))
            n = len(returns)
            if n < 20:
                return None

            window_vol = 20
            window_corr = 5
            min_window = max(window_vol, window_corr + 1)

            feat_len = n - min_window
            if feat_len < MIN_ROWS:
                return None

            vols = np.array([np.std(returns[i : i + window_vol]) for i in range(feat_len)])
            autocorrs = np.array([self._autocorr(returns[i : i + window_corr + 1], lag=1) for i in range(feat_len)])
            fg_arr = np.full(feat_len, fear_greed)

            # 标准化（避免 HMM 收敛问题）
            def safe_normalize(arr):
                std = arr.std()
                return (arr - arr.mean()) / std if std > 1e-10 else arr - arr.mean()

            features = np.column_stack(
                [
                    safe_normalize(vols),
                    safe_normalize(autocorrs),
                    fg_arr,
                ]
            )
            return features

        except Exception as e:
            logger.warning(f"_build_features error: {e}")
            return None

    @staticmethod
    def _autocorr(arr: np.ndarray, lag: int = 1) -> float:
        if len(arr) <= lag:
            return 0.0
        x = arr[:-lag]
        y = arr[lag:]
        if x.std() < 1e-10 or y.std() < 1e-10:
            return 0.0
        from oprim import pearson_spearman_corr

        result = pearson_spearman_corr(x, y, min_samples=2)
        return float(result["pearson_r"])

    # ── HMM 训练 + 预测（同步，在线程池执行）────────────
    def _fit_and_predict(self, features: np.ndarray) -> tuple[str, float, dict]:
        try:
            from hmmlearn import hmm as hmmlearn_hmm

            model = hmmlearn_hmm.GaussianHMM(
                n_components=N_COMPONENTS,
                covariance_type="diag",
                n_iter=N_ITER,
                random_state=42,
            )
            model.fit(features)

            # 预测当前隐状态
            states = model.predict(features)
            curr_state = int(states[-1])

            # 置信度：当前时刻的后验概率最大值
            posteriors = model.predict_proba(features)
            confidence = float(posteriors[-1].max())

            # 给隐状态打语义标签
            means = model.means_  # shape (N_COMPONENTS, 3)
            vol_means = means[:, 0]  # 特征0: 波动率（已标准化）
            autocorr_means = means[:, 1]  # 特征1: 自相关

            # 最高波动率 → crisis
            crisis_idx = int(np.argmax(vol_means))

            # 其余两个按自相关排序
            remaining = [i for i in range(N_COMPONENTS) if i != crisis_idx]
            r0, r1 = remaining
            trend_idx = r0 if autocorr_means[r0] >= autocorr_means[r1] else r1
            range_idx = r1 if trend_idx == r0 else r0

            label_map = {
                crisis_idx: "crisis",
                trend_idx: "trend",
                range_idx: "range",
            }
            state_name = label_map.get(curr_state, "range")

            # 特征摘要（用于监控）
            last_feat = features[-1]
            feat_summary = {
                "realized_vol_norm": round(float(last_feat[0]), 3),
                "price_autocorr_norm": round(float(last_feat[1]), 3),
                "fear_greed_norm": round(float(last_feat[2]), 3),
                "state_idx": curr_state,
                "label_map": {str(k): v for k, v in label_map.items()},
            }
            return state_name, confidence, feat_summary

        except ImportError:
            logger.warning("hmmlearn 未安装，HMM 检测回退到 range。安装方法：pip install hmmlearn")
            return "range", 0.0, {}
        except Exception as e:
            logger.error(f"HMM fit error: {e}")
            return "range", 0.0, {}

    # ── Redis 读写 ────────────────────────────────────────
    async def _write_result(
        self,
        symbol: str,
        state: str,
        confidence: float,
        feat_summary: dict,
    ):
        result = {
            "state": state,
            "confidence": round(confidence, 4),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "features": feat_summary,
        }
        key = f"{REDIS_KEY_PREFIX}:{symbol}"
        await self._r.set(key, json.dumps(result), ex=REDIS_TTL)

    # ── 数据加载 ──────────────────────────────────────────
    async def _load_closes(self, symbol: str) -> Optional[np.ndarray]:
        try:
            async with self._pg.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT close FROM candles
                    WHERE symbol = $1 AND interval = '1h'
                    ORDER BY time DESC
                    LIMIT $2
                """,
                    symbol,
                    LOOKBACK_BARS,
                )
        except Exception as e:
            logger.error(f"_load_closes {symbol}: {e}")
            return None

        if not rows:
            return None
        # 反转为时间正序
        return np.array([float(r["close"]) for r in reversed(rows)])

    async def _load_fear_greed(self) -> float:
        try:
            raw = await self._r.get("sentiment:fear_greed")
            if not raw:
                return 0.5
            d = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
            return float(d.get("value", 50)) / 100.0
        except Exception:
            return 0.5


# ── 公共接口：读取 HMM 状态 ──────────────────────────────


async def get_hmm_regime(redis_client, symbol: str) -> dict:
    """
    读取指定 symbol 的 HMM Regime 状态。
    HMM 服务未就绪时返回默认 range。
    """
    try:
        raw = await redis_client.get(f"{REDIS_KEY_PREFIX}:{symbol}")
        if not raw:
            return {"state": "range", "confidence": 0.0, "source": "default"}
        d = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
        d["source"] = "hmm"
        return d
    except Exception as e:
        logger.warning(f"get_hmm_regime {symbol}: {e}")
        return {"state": "range", "confidence": 0.0, "source": "error"}


# ── 核心融合函数：HMM + ADX+ATR → 最终 Regime + Kelly 系数 ──


def fuse_regimes(
    hmm_state: str,
    hmm_conf: float,
    adxatr_regime,  # shared.models.signal.Regime 枚举值
    adxatr_bars: int = 0,  # 当前 regime 持续多少根K线（稳定性指标）
) -> tuple[str, float]:
    """
    两层融合逻辑：
      HMM 决定中期框架（慢信号，权重60%）
      ADX+ATR 在框架内做短期确认（快信号，权重40%）

    返回 (final_regime_str, kelly_scalar)
      kelly_scalar: 对 Kelly 系数的额外乘数（0.1 ~ 1.3）

    映射表（HMM × ADX+ATR → 最终状态 + Kelly系数）：

    HMM=crisis：无论 ADX+ATR 如何，直接限制
      → final="crisis"  kelly=0.1

    HMM=trend：
      + ADX+ATR=TRENDING_UP/DOWN → "trend_confirmed"  kelly=1.3
      + ADX+ATR=RANGING          → "trend_weak"        kelly=0.8
      + ADX+ATR=HIGH_VOLATILITY  → "trend_volatile"    kelly=0.5
      + ADX+ATR=ACCUMULATION     → "trend_early"       kelly=1.0
      + ADX+ATR=UNKNOWN          → "trend"              kelly=0.9

    HMM=range：
      + ADX+ATR=RANGING          → "range_confirmed"   kelly=1.0
      + ADX+ATR=TRENDING_UP/DOWN → "breakout_watch"    kelly=0.6
      + ADX+ATR=HIGH_VOLATILITY  → "range_volatile"    kelly=0.3
      + 其他                     → "range"              kelly=0.7
    """
    from shared.models.signal import Regime

    # HMM crisis 直接覆盖一切
    if hmm_state == "crisis":
        return "crisis", 0.1

    # ADX+ATR 极高波动也要保护
    if adxatr_regime == Regime.HIGH_VOLATILITY:
        if hmm_state == "trend":
            return "trend_volatile", 0.5
        return "range_volatile", 0.3

    # HMM=trend
    if hmm_state == "trend":
        if adxatr_regime in (Regime.TRENDING_UP, Regime.TRENDING_DOWN):
            scalar = 1.3
            # ADX+ATR 稳定（持续>3根）再给满权
            if adxatr_bars < 3:
                scalar = 1.1
            return "trend_confirmed", scalar
        if adxatr_regime == Regime.ACCUMULATION:
            return "trend_early", 1.0
        if adxatr_regime == Regime.RANGING:
            return "trend_weak", 0.8
        return "trend", 0.9

    # HMM=range
    if hmm_state == "range":
        if adxatr_regime in (Regime.TRENDING_UP, Regime.TRENDING_DOWN):
            # HMM 说震荡但 ADX+ATR 说趋势 → 可能即将突破，谨慎
            return "breakout_watch", 0.6
        if adxatr_regime == Regime.RANGING:
            return "range_confirmed", 1.0
        if adxatr_regime == Regime.ACCUMULATION:
            return "range_accumulation", 0.8
        return "range", 0.7

    # 兜底
    return "unknown", 0.5
