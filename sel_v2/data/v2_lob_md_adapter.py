"""v2 lob md-adapter — 从统一采集层 iris 的 md.orderbook_features(P3)回填 v2_lob_snapshots。

采集层合并(方案 B / P3):Binance LOB 微观特征不再由 selene 自采(v2_lob_collector 直打
Binance /fapi/v1/depth),改读中立生产者 iris 统一采集写规范表 `marketdata.md.orderbook_features`
(venue=binance)的 L1+top5 聚合特征(best/mid/microprice/spread/depth5/imbalance/entropy5,后者
用与原 v2_lob_collector.calc_entropy 相同的 top5/log2 公式,历史熵序列保持连续)。

iris 只产聚合特征,不产原始逐档 bids/asks——下游(entropy_pctile/lob_depth_pctile/Coiling 的
entropy_low 条件,见 paper_engine.py 的 AVG(entropy)/(bid_depth+ask_depth))只消费 bid_depth/
ask_depth/entropy 三个聚合量,v2_lob_snapshots.bids/asks 原始 JSONB 全仓库无消费者(已核实),
故本 adapter 写占位空数组 '[]'(NOT NULL 约束要求非 NULL;诚实标注"无原始档位"而非伪造档位数据)。

纯 DB→DB(md 与 selene 同在 platform-postgres),无 egress/不需代理。按 timestamp 做增量水位
(watermark)同步而非每轮全表扫描——LOB 是最高频的表,增量策略避免随历史增长而变慢,同时冷启动
时自然从 iris 已采集的最早一行开始追,天然完成"历史回填"(iris 采多久,selene 就能追多久)。

替代 v2_lob_collector 的 REST 轮询循环(保留其文件供回滚:compose command 切回即可)。
"""

from __future__ import annotations

import argparse
import asyncio
import os

import asyncpg

MD_DSN = os.environ.get(
    "MD_DSN", "postgresql://selene_app:sel_app_2026@platform-postgres:5432/marketdata"
)
DB_URL = os.environ.get(
    "DB_URL", "postgresql://selene_app:sel_app_2026@platform-postgres:5432/selene"
)
VENUE = os.environ.get("LOB_MD_VENUE", "binance")
BASE_SYMBOL = os.environ.get("SYMBOLS", "BTC-USDT")
_BATCH_LIMIT = 5000


async def sync(md: asyncpg.Pool, sel: asyncpg.Pool, last_ts) -> tuple[int, object]:
    rows = await md.fetch(
        """
        SELECT ts, bid_depth5, ask_depth5, entropy5
        FROM md.orderbook_features
        WHERE venue=$1 AND symbol=$2 AND ts > $3
        ORDER BY ts ASC
        LIMIT $4
        """,
        VENUE,
        BASE_SYMBOL,
        last_ts,
        _BATCH_LIMIT,
    )
    if not rows:
        return 0, last_ts
    records = [
        (
            r["ts"],
            BASE_SYMBOL,
            "[]",
            "[]",
            r["bid_depth5"],
            r["ask_depth5"],
            r["entropy5"],
        )
        for r in rows
    ]
    await sel.executemany(
        """
        INSERT INTO v2_lob_snapshots (timestamp, symbol, bids, asks, bid_depth, ask_depth, entropy)
        VALUES ($1,$2,$3::jsonb,$4::jsonb,$5,$6,$7)
        ON CONFLICT (timestamp, symbol) DO NOTHING
        """,
        records,
    )
    return len(records), rows[-1]["ts"]


async def _run(loop_s: int) -> None:
    md = await asyncpg.create_pool(MD_DSN, min_size=1, max_size=2)
    sel = await asyncpg.create_pool(DB_URL, min_size=1, max_size=2)
    print(
        f"v2_lob_md_adapter start: {VENUE} orderbook from md.orderbook_features, "
        f"loop={loop_s}s",
        flush=True,
    )
    try:
        while True:
            try:
                last_ts = await sel.fetchval(
                    "SELECT COALESCE(MAX(timestamp), '-infinity'::timestamptz) "
                    "FROM v2_lob_snapshots WHERE symbol=$1",
                    BASE_SYMBOL,
                )
                n, _ = await sync(md, sel, last_ts)
                print(
                    f"v2_lob_md_adapter: {n} {VENUE} orderbook rows upserted "
                    f"(watermark was {last_ts})",
                    flush=True,
                )
            except Exception as exc:  # noqa: BLE001
                print(f"v2_lob_md_adapter error: {exc}", flush=True)
            if loop_s <= 0:
                break
            await asyncio.sleep(loop_s)
    finally:
        await md.close()
        await sel.close()


def main() -> None:
    p = argparse.ArgumentParser(
        description="v2_lob_snapshots from iris md.orderbook_features(binance)"
    )
    p.add_argument("--loop", type=int, default=20, help="poll seconds (0 = one-shot)")
    args = p.parse_args()
    asyncio.run(_run(args.loop))


if __name__ == "__main__":
    main()
