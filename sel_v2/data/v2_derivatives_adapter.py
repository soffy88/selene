"""v2 derivatives adapter — 从统一采集层 iris 的 md:* 读取,回填 v2_derivatives_snapshots.

采集层合并(方案 B):funding/OI 不再由 selene 自采,改由中立生产者 iris 统一采集 Binance,
写规范 key `md:funding:binance:{sym}` / `md:oi:binance:{sym}`(helios-redis /1)。本 adapter 把
这些规范 key 映射回 selene 原表 v2_derivatives_snapshots,**下游(ofi_persister/信号)一行不用改**。

替代 v2_derivatives_collector(保留其文件供回滚:compose `command` 切回即可)。口径已对账一致
(iris vs selene 同源 Binance:funding 精确相等、mark/index/oi 漂移 <0.02%),见 iris/tools/reconcile.py。
"""

import asyncio
import logging
import os
from datetime import datetime

import asyncpg
import redis.asyncio as aioredis

from sel_v2.db.migrations import apply_schema
from sel_v2.data.insert_guard import InsertGuard, InsertFailureLimitExceeded

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("v2_derivatives_adapter")

_guard = InsertGuard("v2_derivatives_snapshots")

DB_URL = os.environ.get("DB_URL")
BASE_SYMBOL = os.environ.get("SYMBOLS", "BTC-USDT")  # stored symbol
# 统一采集层的规范 key 所在(helios-redis /1,md:* 与 diagnostic:* 前缀隔离)
MD_REDIS_URL = os.environ.get("MD_REDIS_URL", "redis://helios-redis:6379/1")
VENUE = "binance"


def _f(h: dict, key: str):
    """float(h[key]) or None —— 缺失/空串(规范层用 "" 表示 NULL)一律 None,绝不 0.0。"""
    v = h.get(key)
    if v in (None, ""):
        return None
    return float(v)


def build_row_from_md(funding: dict, oi: dict, symbol: str):
    """把 md:funding + md:oi 两个 hash 映射成 v2_derivatives_snapshots 行,或 None(funding 缺失)。

    行 = (timestamp, symbol, funding_rate, open_interest, mark_price, index_price),与旧采集器一致。
    - timestamp   ← funding.updated_at(= Binance premiumIndex time,与旧采集器同语义)
    - open_interest ← oi.oi_coin(Binance openInterest 币本位,与旧采集器同口径,对账已确认相等)
    """
    if not funding:
        return None
    ts_raw = funding.get("updated_at")
    if not ts_raw:
        return None
    ts = datetime.fromisoformat(ts_raw)
    return (
        ts,
        symbol,
        _f(funding, "funding_rate"),
        _f(oi, "oi_coin") if oi else None,
        _f(funding, "mark_price"),
        _f(funding, "index_price"),
    )


async def main():
    pool = await asyncpg.create_pool(DB_URL)
    await apply_schema(pool)
    r = aioredis.from_url(MD_REDIS_URL, decode_responses=True)
    fkey = f"md:funding:{VENUE}:{BASE_SYMBOL}"
    okey = f"md:oi:{VENUE}:{BASE_SYMBOL}"
    insert_count = 0
    logger.info(
        f"derivatives adapter: reading {fkey} + {okey} from {MD_REDIS_URL} "
        f"-> v2_derivatives_snapshots (stored as {BASE_SYMBOL})"
    )

    while True:
        try:
            funding = await r.hgetall(fkey)
            oi = await r.hgetall(okey)
            # md hash 有 300s TTL:iris 若停,key 过期 -> hgetall 空 -> 跳过,绝不写陈旧行
            row = build_row_from_md(funding, oi, BASE_SYMBOL)
            if row is not None:
                try:
                    await pool.execute(
                        "INSERT INTO v2_derivatives_snapshots (timestamp, symbol, funding_rate, open_interest, mark_price, index_price) VALUES ($1, $2, $3, $4, $5, $6) ON CONFLICT DO NOTHING",
                        *row,
                    )
                    _guard.ok()
                    insert_count += 1
                    if insert_count == 1:
                        logger.info(
                            "INSERT row 1 to v2_derivatives_snapshots (via md adapter)"
                        )
                    if insert_count % 10 == 0:
                        logger.info(
                            f"v2_derivatives_snapshots cumulative inserts: {insert_count}"
                        )
                except Exception as db_e:
                    _guard.fail(db_e)
        except InsertFailureLimitExceeded:
            raise  # fail-fast: let the process exit so the fault surfaces
        except Exception as e:
            logger.error(f"Error reading md keys: {e}")

        await asyncio.sleep(30)


if __name__ == "__main__":
    asyncio.run(main())
