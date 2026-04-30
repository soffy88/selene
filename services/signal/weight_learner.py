"""
services/signal/weight_learner.py

移植自 quant-stack/services/prob-engine/src/weight_learner.py
适配 cryptowatch v4 的 MultiFactorScorer 因子权重体系

原版学习的是"引擎"权重（qlib/tradingview等）。
v4 版学习的是"因子"权重（technical_rsi/funding_zscore/onchain等）。

核心逻辑完全相同：
  1. 每小时从 TimescaleDB 读近30天成交记录
  2. 计算每个因子信号方向 vs 实际盈亏的 EWMA 准确率
  3. 权重 = base_weight × (0.5 + accuracy)，上下限 ±50%
  4. 写入 Redis，signal-service 下次评分时读取

Redis Keys：
  cw4:weight_learner:{factor}:accuracy  → EWMA准确率 (0~1)
  cw4:weight_learner:{factor}:weight    → 动态权重
  cw4:weight_learner:updated_at         → 最后更新时间
  cw4:weight_learner:status             → 运行状态摘要

因子列表（与 composite.py 的 WEIGHTS 对齐）：
  technical_rsi / technical_ema / funding_zscore /
  oi_momentum / lsr_divergence / onchain / social / orderbook
"""

import asyncio
import json
import logging
import math
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger("signal.weight_learner")

# ── 超参数 ────────────────────────────────────────────
EWMA_LAMBDA     = float(os.getenv("WL_EWMA_LAMBDA",    "0.90"))
MIN_TRADES      = int(os.getenv("WL_MIN_TRADES",        "10"))
UPDATE_INTERVAL = int(os.getenv("WL_UPDATE_INTERVAL",   "3600"))  # 秒
LOOKBACK_DAYS   = int(os.getenv("WL_LOOKBACK_DAYS",     "30"))
REDIS_TTL       = 86400 * 3   # Redis key 3天有效期
# 归一化 pnl / notional 尺度（0.02 = 2% 移动就达到满权）
PNL_SCALE       = float(os.getenv("WL_PNL_SCALE",      "0.02"))

# 因子基础权重（与 composite.py WEIGHTS 完全对齐）
BASE_WEIGHTS: dict[str, float] = {
    "technical_rsi":   0.15,
    "technical_ema":   0.15,
    "funding_zscore":  0.20,
    "oi_momentum":     0.15,
    "lsr_divergence":  0.10,
    "onchain":         0.10,
    "social":          0.10,
    "orderbook":       0.05,
}

REDIS_PREFIX = "cw4:weight_learner"


class WeightLearner:
    """
    异步后台协程，定期学习各因子的预测准确率并更新动态权重。
    在 signal-service 的 lifespan 中 create_task 启动。
    """

    def __init__(self, redis_client, pg_pool):
        self._r  = redis_client
        self._pg = pg_pool
        self._running = False

    async def run(self):
        """主循环，每 UPDATE_INTERVAL 秒更新一次"""
        self._running = True
        logger.info(
            f"WeightLearner 启动 "
            f"interval={UPDATE_INTERVAL}s ewma_lambda={EWMA_LAMBDA}"
        )

        # 启动时立即跑一次
        try:
            await self._update()
        except Exception as e:
            logger.warning(f"初次更新失败（正常，等待成交数据积累）: {e}")

        while self._running:
            await asyncio.sleep(UPDATE_INTERVAL)
            try:
                await self._update()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"weight_learner update error: {e}", exc_info=True)

    def stop(self):
        self._running = False

    # ── 核心更新 ──────────────────────────────────────────
    async def _update(self):
        trades = await self._load_trades()
        if not trades or len(trades) < MIN_TRADES:
            logger.debug(
                f"成交记录不足（{len(trades) if trades else 0}/{MIN_TRADES}），跳过更新"
            )
            return

        # 计算各因子的（pnl 加权）correctness 向量
        # outcome ∈ [0, 1]：用 tanh(pnl_ratio / PNL_SCALE) 把收益幅度归一
        factor_results: dict[str, list[float]] = {f: [] for f in BASE_WEIGHTS}

        for trade in trades:
            pnl            = float(trade.get("pnl",      0.0))
            notional       = float(trade.get("notional", 0.0))
            factor_scores  = trade.get("factor_scores", {})
            if notional <= 0:
                continue

            pnl_ratio = pnl / notional
            mag       = math.tanh(abs(pnl_ratio) / PNL_SCALE)   # 0..1

            for factor in BASE_WEIGHTS:
                fs = float(factor_scores.get(factor, 0.0))
                if abs(fs) < 0.01:
                    continue                         # 因子中性，不计入
                predicted_up = fs > 0
                actual_up    = pnl > 0
                raw_correct  = 1.0 if predicted_up == actual_up else 0.0
                # 用 pnl 幅度加权：0.5 + (correct - 0.5) * mag
                # 小收益 → 靠近 0.5（低信息），大收益 → 靠近 0/1
                weighted = 0.5 + (raw_correct - 0.5) * mag
                factor_results[factor].append(weighted)

        # EWMA 更新准确率 → 计算动态权重 → 写 Redis
        pipe = self._r.pipeline()
        updated = {}

        for factor, results in factor_results.items():
            if not results:
                continue

            # 读历史 EWMA 准确率
            raw_acc = await self._r.get(f"{REDIS_PREFIX}:{factor}:accuracy")
            old_acc = float(raw_acc) if raw_acc else 0.5   # 默认0.5（无先验）

            # EWMA 更新
            new_acc = old_acc
            for r in results:
                new_acc = EWMA_LAMBDA * new_acc + (1 - EWMA_LAMBDA) * r
            new_acc = round(max(0.0, min(1.0, new_acc)), 4)

            # 动态权重（准确率0.5→不变，0→半权，1.0→1.5倍）
            base_w = BASE_WEIGHTS[factor]
            dyn_w  = round(base_w * (0.5 + new_acc), 4)
            dyn_w  = round(max(base_w * 0.5, min(base_w * 1.5, dyn_w)), 4)

            pipe.setex(f"{REDIS_PREFIX}:{factor}:accuracy", REDIS_TTL, str(new_acc))
            pipe.setex(f"{REDIS_PREFIX}:{factor}:weight",   REDIS_TTL, str(dyn_w))
            updated[factor] = {"acc": new_acc, "weight": dyn_w, "n": len(results)}

            logger.debug(
                f"factor={factor} "
                f"acc: {old_acc:.3f}→{new_acc:.3f} "
                f"weight: {base_w:.3f}→{dyn_w:.3f} "
                f"n={len(results)}"
            )

        # 写状态摘要
        status = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "n_trades":   len(trades),
            "factors":    updated,
        }
        pipe.setex(f"{REDIS_PREFIX}:updated_at", REDIS_TTL,
                   datetime.now(timezone.utc).isoformat())
        pipe.setex(f"{REDIS_PREFIX}:status", REDIS_TTL,
                   json.dumps(status, ensure_ascii=False))
        await pipe.execute()

        logger.info(
            f"WeightLearner 更新完成 "
            f"trades={len(trades)} "
            f"factors_updated={len(updated)}"
        )

    # ── 从 TimescaleDB 读成交+因子记录 ───────────────────
    async def _load_trades(self) -> list[dict]:
        """
        从 audit_log 读取近 N 天的成交记录。
        audit_log.payload 里包含 factor_scores（由 signal-service 在发布信号时写入）。
        """
        since = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
        try:
            async with self._pg.acquire() as conn:
                rows = await conn.fetch("""
                    SELECT
                        a.payload,
                        o.realized_pnl,
                        COALESCE(o.filled_price, o.limit_price) AS entry_price,
                        COALESCE(o.filled_qty,   o.quantity)    AS qty
                    FROM audit_log a
                    JOIN orders o
                      ON (a.payload->>'signal_id') = o.signal_id::text
                    WHERE a.event_type = 'ORDER_FILLED'
                      AND a.time >= $1
                      AND o.realized_pnl IS NOT NULL
                      AND a.payload ? 'factor_scores'
                    ORDER BY a.time ASC
                """, since)
        except Exception as e:
            logger.warning(f"_load_trades DB error: {e}")
            return []

        result = []
        for row in rows:
            try:
                payload = row["payload"]
                if isinstance(payload, str):
                    payload = json.loads(payload)
                pnl      = float(row["realized_pnl"] or 0)
                entry    = float(row["entry_price"]  or 0)
                qty      = float(row["qty"]          or 0)
                notional = entry * qty
                result.append({
                    "pnl":           pnl,
                    "notional":      notional,
                    "factor_scores": payload.get("factor_scores", {}),
                })
            except Exception:
                pass
        return result


# ── 公共接口：signal-service 读取动态权重 ─────────────────

async def read_dynamic_weights(redis_client) -> dict[str, float]:
    """
    从 Redis 读取学习后的动态权重。
    首次启动 Redis 无数据时，回退到 BASE_WEIGHTS。
    供 signal/main.py 的 _score_and_emit() 调用。
    """
    result = {}
    for factor, base_w in BASE_WEIGHTS.items():
        try:
            raw = await redis_client.get(f"{REDIS_PREFIX}:{factor}:weight")
            result[factor] = float(raw) if raw else base_w
        except Exception:
            result[factor] = base_w
    return result


async def get_learner_status(redis_client) -> dict:
    """供 /api/v4/signals/weights 接口展示当前学习状态"""
    raw = await redis_client.get(f"{REDIS_PREFIX}:status")
    if not raw:
        return {
            "status":    "no_data",
            "msg":       "尚未完成首次学习，使用基础权重",
            "base_weights": BASE_WEIGHTS,
        }
    try:
        return json.loads(raw.decode() if isinstance(raw, bytes) else raw)
    except Exception:
        return {"status": "parse_error"}
