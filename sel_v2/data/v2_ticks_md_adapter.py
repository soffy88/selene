"""v2 ticks md-adapter — 从统一采集层 iris 的 md.trades(P4)回填 v2_ticks。

采集层合并(方案 B / P4):逐笔成交不再由 selene 自采(v2_tick_collector 直连 OKX WS
trades 频道),改读中立生产者 iris 的 md.trades(venue=okx——实测 Binance WS 经
helios-proxy 不通,只有 OKX 经 host.docker.internal:10808 隧道验证可行,见
iris/collectors/ws_stream.py)。iris 用同一条 WS 连接同时采 P4 trades 与 P5
liquidation-orders(见 v2_liquidations_md_adapter.py)。

纯 DB→DB(md 与 selene 同在 platform-postgres),无 egress/不需代理。按 trade_id 做增量
水位(而非 timestamp——同一毫秒内可能有多笔成交,ts 水位会在边界丢单;OKX tradeId 是
按 instrument 单调递增的数字字符串,可安全转 bigint 排序,不会像 LOB/衍生品那样在
"同一时刻多行"上产生歧义)。

替代 v2_tick_collector 的 WS 订阅循环(保留其文件供回滚:compose command 切回即可)。
"""

from __future__ import annotations

import argparse
import asyncio
import os

import asyncpg

MD_DSN = os.environ.get("MD_DSN", "postgresql://selene_app:sel_app_2026@platform-postgres:5432/marketdata")
DB_URL = os.environ.get("DB_URL", "postgresql://selene_app:sel_app_2026@platform-postgres:5432/selene")
VENUE = os.environ.get("TICKS_MD_VENUE", "okx")
BASE_SYMBOL = os.environ.get("SYMBOLS", "BTC-USDT")
_BATCH_LIMIT = 5000


async def sync(md: asyncpg.Pool, sel: asyncpg.Pool, last_trade_id: int) -> tuple[int, int]:
    rows = await md.fetch(
        """
        SELECT ts, trade_id, price, size, side
        FROM md.trades
        WHERE venue=$1 AND symbol=$2 AND trade_id::bigint > $3
        ORDER BY trade_id::bigint ASC
        LIMIT $4
        """,
        VENUE,
        BASE_SYMBOL,
        last_trade_id,
        _BATCH_LIMIT,
    )
    if not rows:
        return 0, last_trade_id
    records = [(r["ts"], BASE_SYMBOL, r["price"], r["size"], r["side"], r["trade_id"]) for r in rows]
    await sel.executemany(
        """
        INSERT INTO v2_ticks (timestamp, symbol, price, size, side, trade_id)
        VALUES ($1,$2,$3,$4,$5,$6)
        ON CONFLICT (timestamp, symbol, trade_id) DO NOTHING
        """,
        records,
    )
    return len(records), int(rows[-1]["trade_id"])


async def _run(loop_s: int) -> None:
    md = await asyncpg.create_pool(MD_DSN, min_size=1, max_size=2)
    sel = await asyncpg.create_pool(DB_URL, min_size=1, max_size=2)
    print(
        f"v2_ticks_md_adapter start: {VENUE} trades from md.trades, loop={loop_s}s",
        flush=True,
    )
    try:
        while True:
            try:
                last_trade_id = await sel.fetchval(
                    """
                    SELECT COALESCE(MAX(trade_id::bigint), 0) FROM v2_ticks
                    WHERE symbol=$1 AND trade_id ~ '^[0-9]+$'
                    """,
                    BASE_SYMBOL,
                )
                n, _ = await sync(md, sel, last_trade_id)
                print(
                    f"v2_ticks_md_adapter: {n} {VENUE} trades upserted (watermark was trade_id>{last_trade_id})",
                    flush=True,
                )
            except Exception as exc:  # noqa: BLE001
                print(f"v2_ticks_md_adapter error: {exc}", flush=True)
            if loop_s <= 0:
                break
            await asyncio.sleep(loop_s)
    finally:
        await md.close()
        await sel.close()


def main() -> None:
    p = argparse.ArgumentParser(description="v2_ticks from iris md.trades(okx)")
    p.add_argument("--loop", type=int, default=5, help="poll seconds (0 = one-shot)")
    args = p.parse_args()
    asyncio.run(_run(args.loop))


if __name__ == "__main__":
    main()
