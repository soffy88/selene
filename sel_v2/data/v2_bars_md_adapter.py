"""v2 bars md-adapter — 从统一采集层 iris 的 md.ohlcv(binance,4h)回填 v2_bars_4h。

采集层合并(方案 B / P2b):Binance 4H bar 不再由 selene 自采(binance_backfill 直打 fapi/klines),
改由中立生产者 iris 统一采集 Binance USD-M perp,写规范表 `marketdata.md.ohlcv`
(venue=binance, instrument_type=perp, timeframe=h4)。本 adapter 把 md 行映射回 selene 的
v2_bars_4h,**下游(state machine/replay/Hawkes/TDA/cockpit)一行不用改**。

替代 binance_backfill 的 `--loop`(保留其文件供回滚 + 深史一次性回填:compose command 切回即可)。
纯 DB→DB(md 与 selene 同在 platform-postgres),**无 egress / 不需代理**。

口径映射(md.ohlcv → v2_bars_4h, 与 binance_backfill 逐字一致):
  - time   = bar_open_ts(= klines openTime, selene bar 键);symbol = 归一化 base('BTC-USDT')
  - o/h/l/c/volume 直传(volume=coin 口径);vwap = NULL(klines 无 VWAP,不伪造 0.0,audit P2-5)
  - tick_count = 0(标记 REST/klines-recovered, 同 aggregator 约定);source = 'binance'
  - ON CONFLICT (time, symbol) DO UPDATE **WHERE source='binance'** —— tick 聚合的真 bar
    (source!='binance')永不覆盖;但陈旧的 binance bar 用 md 刷新(修 binance_backfill 的既有
    隐患:它捕获成形中 bar 后 DO NOTHING 永不更新 → 最近 bar 恒残缺;md 因 is_final upsert 而准确)。

md binance 4h 深度取决于 iris 采集起点;历史深回填仍可手动 `binance_backfill --years N`(旧文件保留)。
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


async def sync(md: asyncpg.Pool, sel: asyncpg.Pool) -> int:
    rows = await md.fetch(
        """
        SELECT symbol, bar_open_ts, open, high, low, close, volume
        FROM md.ohlcv
        WHERE venue='binance' AND instrument_type='perp' AND timeframe='h4'
        ORDER BY bar_open_ts ASC
        """
    )
    if not rows:
        return 0
    records = [
        (
            r["bar_open_ts"],
            r["symbol"],
            r["open"],
            r["high"],
            r["low"],
            r["close"],
            r["volume"],
            None,  # vwap: klines 无 VWAP
            0,  # tick_count: REST/klines-recovered
            "binance",
        )
        for r in rows
    ]
    await sel.executemany(
        """
        INSERT INTO v2_bars_4h (time, symbol, open, high, low, close, volume, vwap, tick_count, source)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
        ON CONFLICT (time, symbol) DO UPDATE SET
            open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low,
            close=EXCLUDED.close, volume=EXCLUDED.volume
        WHERE v2_bars_4h.source = 'binance'
        """,
        records,
    )
    return len(records)


async def _run(loop_s: int) -> None:
    md = await asyncpg.create_pool(MD_DSN, min_size=1, max_size=2)
    sel = await asyncpg.create_pool(DB_URL, min_size=1, max_size=2)
    print(
        f"v2_bars_md_adapter start: binance 4h from md.ohlcv, loop={loop_s}s",
        flush=True,
    )
    try:
        while True:
            try:
                n = await sync(md, sel)
                print(f"v2_bars_md_adapter: {n} binance 4h bars upserted", flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"v2_bars_md_adapter error: {exc}", flush=True)
            if loop_s <= 0:
                break
            await asyncio.sleep(loop_s)
    finally:
        await md.close()
        await sel.close()


def main() -> None:
    p = argparse.ArgumentParser(description="v2_bars_4h from iris md.ohlcv(binance,4h)")
    p.add_argument("--loop", type=int, default=0, help="poll seconds (0 = one-shot)")
    args = p.parse_args()
    asyncio.run(_run(args.loop))


if __name__ == "__main__":
    main()
