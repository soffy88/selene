"""
services/signal/onchain/bridge.py

Signal-service 与 onchain-sentinel 的桥接层。

在 signal-service 的评分循环中，每次组装 FactorScores 前调用
`await get_onchain_factor(symbol)` 即可。

消费 signal.raw 中 source=onchain-sentinel 的消息，
触发对应 symbol 的重新评分。
"""

import asyncio
import json
import logging
import time
from typing import Optional

logger = logging.getLogger("signal.onchain_bridge")

# ── signal.raw 中 onchain-sentinel 写入的 factor 更新消费 ─────────────────────

async def consume_onchain_factor_updates(
    rescore_fn,   # async fn(symbol: str) → None，由 signal-service 提供
):
    """
    持续消费 signal.raw 中来自 onchain-sentinel 的 factor 更新消息。
    收到后立即触发对该 symbol 的重新评分，确保 onchain factor 实时纳入。

    在 signal-service 的 main 协程中 create_task 即可：
        asyncio.create_task(consume_onchain_factor_updates(rescore))
    """
    from shared.db.redis_client import get_redis
    from shared.events.streams import STREAM_SIGNAL_RAW, CG_SIGNAL, ensure_consumer_group

    r = get_redis()
    consumer = "signal-onchain-bridge"

    try:
        await r.xgroup_create(STREAM_SIGNAL_RAW, CG_SIGNAL, id="0", mkstream=True)
    except Exception as e:
        if "BUSYGROUP" not in str(e):
            raise

    logger.info("onchain_bridge: listening on signal.raw for factor updates")

    while True:
        try:
            results = await r.xreadgroup(
                CG_SIGNAL, consumer,
                {STREAM_SIGNAL_RAW: ">"},
                count=20, block=1000,
            )
            if not results:
                continue

            for _, messages in results:
                for msg_id, fields in messages:
                    try:
                        data = _decode(fields)
                        # 只处理来自 onchain-sentinel 的 factor 更新
                        if data.get("source") != "onchain-sentinel":
                            await r.xack(STREAM_SIGNAL_RAW, CG_SIGNAL, msg_id)
                            continue

                        symbol = data.get("symbol", "")
                        factor = float(data.get("factor_score", 0))
                        logger.info(
                            f"onchain factor update: {symbol} score={factor:+.4f} "
                            f"regime={data.get('regime')} "
                            f"net_flow={data.get('net_ex_flow')}"
                        )

                        # 触发重评（signal-service 实现这个函数）
                        if symbol and rescore_fn:
                            asyncio.create_task(rescore_fn(symbol))

                        await r.xack(STREAM_SIGNAL_RAW, CG_SIGNAL, msg_id)

                    except Exception as e:
                        logger.error(f"bridge handle error: {e}")

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"consume_onchain_factor_updates: {e}")
            await asyncio.sleep(1)


def _decode(fields: dict) -> dict:
    result = {}
    for k, v in fields.items():
        key = k.decode() if isinstance(k, bytes) else k
        val = v.decode() if isinstance(v, bytes) else v
        try:
            result[key] = json.loads(val)
        except Exception:
            result[key] = val
    return result


# ── 在 signal-service 的 main.py 中集成的示例代码 ─────────────────────────────
#
# from services.signal.onchain.bridge import consume_onchain_factor_updates
# from services.signal.factors.composite import get_onchain_factor
#
# # 在 signal-service 启动时：
# async def rescore(symbol: str):
#     """当 onchain factor 更新时重新触发该 symbol 的评分"""
#     # 取最新的链上因子
#     onchain = await get_onchain_factor(symbol)
#     # 更新内存中的 factor cache，下次评分循环会自动使用
#     _onchain_cache[symbol] = onchain
#     logger.info(f"rescore triggered: {symbol} onchain={onchain:+.4f}")
#
# # 在 lifespan 或 startup 中：
# asyncio.create_task(consume_onchain_factor_updates(rescore))
#
# # 在评分循环中组装 FactorScores 时：
# onchain_score = _onchain_cache.get(symbol, 0.0)
# # 或直接异步读（两种方式都可以）：
# onchain_score = await get_onchain_factor(symbol)
#
# factors = FactorScores(
#     technical_rsi  = score_rsi(rsi),
#     technical_ema  = score_ema_alignment(ema20, ema50, ema200, price),
#     funding_zscore = score_funding_zscore(funding_rate, funding_history),
#     oi_momentum    = score_oi_momentum(oi_change_pct, price_change_pct),
#     lsr_divergence = score_lsr_divergence(long_ratio),
#     onchain        = onchain_score,   # ← 改动：从 0.0 改为动态值
#     social         = 0.0,
#     orderbook      = 0.0,
# )
