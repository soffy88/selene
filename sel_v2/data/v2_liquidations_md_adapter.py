"""v2 liquidations md-adapter — 从统一采集层 iris 的 md.liquidations(P5)回填 v2_liquidations。

采集层合并(方案 B / P5):清算事件不再由 selene 自采(v2_liquidation_collector 直连 OKX WS
liquidation-orders 频道),改读中立生产者 iris 的 md.liquidations(venue=okx,与 P4 trades
共用同一条 WS 连接,见 iris/collectors/ws_stream.py)。

纯 DB→DB,无 egress/不需代理。清算事件无逐笔 id,按 timestamp 做增量水位(同 LOB
adapter——清算频率远低于 trades,同一时刻多行的水位边界丢单概率可忽略,不需要 trade_id
那种强序号)。

price/loss 在 iris 侧允许为 NULL(venue 未提供时不伪造),但 v2_liquidations 两列都是
NOT NULL——按 selene 旧采集器的既有口径(`float(d["bkPx"]) if d.get("bkPx") else 0.0`)
在这里补 0.0,保持下游行为不变(旧采集器从来不会写 NULL 到这两列)。

替代 v2_liquidation_collector 的 WS 订阅循环(保留其文件供回滚:compose command 切回即可)。
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
VENUE = os.environ.get("LIQ_MD_VENUE", "okx")
BASE_SYMBOL = os.environ.get("SYMBOLS", "BTC-USDT")
_BATCH_LIMIT = 5000


async def sync(md: asyncpg.Pool, sel: asyncpg.Pool, last_ts) -> tuple[int, object]:
    rows = await md.fetch(
        """
        SELECT ts, side, size, price, loss
        FROM md.liquidations
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
            r["side"],
            r["size"],
            r["price"] if r["price"] is not None else 0.0,
            r["loss"] if r["loss"] is not None else 0.0,
        )
        for r in rows
    ]
    await sel.executemany(
        """
        INSERT INTO v2_liquidations (timestamp, symbol, side, size, price, loss)
        VALUES ($1,$2,$3,$4,$5,$6)
        ON CONFLICT (timestamp, symbol, side, size, price) DO NOTHING
        """,
        records,
    )
    return len(records), rows[-1]["ts"]


async def _run(loop_s: int) -> None:
    md = await asyncpg.create_pool(MD_DSN, min_size=1, max_size=2)
    sel = await asyncpg.create_pool(DB_URL, min_size=1, max_size=2)
    print(
        f"v2_liquidations_md_adapter start: {VENUE} liquidations from md.liquidations, "
        f"loop={loop_s}s",
        flush=True,
    )
    try:
        while True:
            try:
                last_ts = await sel.fetchval(
                    "SELECT COALESCE(MAX(timestamp), '-infinity'::timestamptz) "
                    "FROM v2_liquidations WHERE symbol=$1",
                    BASE_SYMBOL,
                )
                n, _ = await sync(md, sel, last_ts)
                print(
                    f"v2_liquidations_md_adapter: {n} {VENUE} liquidations upserted "
                    f"(watermark was {last_ts})",
                    flush=True,
                )
            except Exception as exc:  # noqa: BLE001
                print(f"v2_liquidations_md_adapter error: {exc}", flush=True)
            if loop_s <= 0:
                break
            await asyncio.sleep(loop_s)
    finally:
        await md.close()
        await sel.close()


def main() -> None:
    p = argparse.ArgumentParser(
        description="v2_liquidations from iris md.liquidations(okx)"
    )
    p.add_argument("--loop", type=int, default=15, help="poll seconds (0 = one-shot)")
    args = p.parse_args()
    asyncio.run(_run(args.loop))


if __name__ == "__main__":
    main()
