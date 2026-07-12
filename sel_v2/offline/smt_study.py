"""ICT-8 SMT divergence 实证 (2026-07-12, optimization B1) — BTC/ETH 4H, 2yr.

数据解锁:iris md 层已采 ETH tick;ETH-USDT 4H 深史经 binance_backfill 一次性
回填进 v2_bars_4h(symbol 分区,4380 bars)。SMT 是 2026-07-11 ICT 轮因"没数据"
跳过的唯一概念独特候选(跨资产摆动背离 = 单边流动性工程的痕迹)。

预注册(见 ict_smt.py):双资产同 1.5×ATR zigzag,枢轴配对窗 ±6 bar,事件时刻 =
双方确认的 max(因果);检验 = bearish SMT → BTC 前向 6-bar 收益 < 基准 /
bullish → >(分方向 MW;pass=双方向 p<0.10,单方向=partial —— ICT 轮同规则)。

    python -m sel_v2.offline.smt_study

Offline-only;不碰 strategies/** / states/** / epoch。
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import asyncpg
import numpy as np

from sel_v2.offline.ict_advanced import fwd_return
from sel_v2.offline.ict_smt import MATCH_WINDOW, detect_smt
from sel_v2.offline.lens_common import compute_atr, mann_whitney_one_sided
from sel_v2.offline.lens_study import MIN_GROUP_N

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("smt_study")

REPORT_PATH = Path(__file__).resolve().parents[2] / "analysis" / "smt_v1.md"


async def _load_pair(pool):
    """BTC and ETH 4H bars inner-joined on bar time (aligned index-for-index)."""
    rows = await pool.fetch(
        """
        SELECT b.time, b.high AS bh, b.low AS bl, b.close AS bc,
               e.high AS eh, e.low AS el, e.close AS ec
        FROM v2_bars_4h b
        JOIN v2_bars_4h e ON e.time = b.time AND e.symbol = 'ETH-USDT'
        WHERE b.symbol = 'BTC-USDT'
        ORDER BY b.time ASC
        """
    )
    times = [r["time"] for r in rows]
    f = lambda k: np.array([float(r[k]) for r in rows])  # noqa: E731
    return times, f("bh"), f("bl"), f("bc"), f("eh"), f("el"), f("ec")


async def run() -> dict:
    dsn = os.environ["DB_URL"].replace("postgresql+asyncpg://", "postgresql://")
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    try:
        times, bh, bl, bc, eh, el, ec = await _load_pair(pool)
    finally:
        await pool.close()
    n = len(bc)
    logger.info("aligned BTC/ETH bars: %d (%s → %s)", n, times[0], times[-1])

    corr = float(np.corrcoef(np.diff(np.log(bc)), np.diff(np.log(ec)))[0, 1])
    atr_b = compute_atr(bh, bl, bc)
    atr_e = compute_atr(eh, el, ec)
    events = detect_smt(bc, atr_b, ec, atr_e)
    bear = [e for e in events if e.direction == -1]
    bull = [e for e in events if e.direction == 1]
    logger.info("smt events: %d (bear %d / bull %d)", len(events), len(bear), len(bull))

    baseline = np.array([fwd_return(bc, i) for i in range(n - 6)], dtype=float)
    baseline = baseline[np.isfinite(baseline)]

    def _test(evs, want):
        vals = [fwd_return(bc, e.idx) for e in evs]
        vals = [v for v in vals if v is not None]
        res = {"n": len(vals), "mean": float(np.mean(vals)) if vals else None}
        if len(vals) >= MIN_GROUP_N:
            _u, p = mann_whitney_one_sided(vals, baseline, alternative=want)
            res["p"] = p
        return res

    t_bear = _test(bear, "less")
    t_bull = _test(bull, "greater")
    ps = [t_bear.get("p"), t_bull.get("p")]
    if any(p is None for p in ps):
        verdict = "UNDERPOWERED"
    else:
        hits = sum(1 for p in ps if p < 0.10)
        verdict = "pass" if hits == 2 else "partial" if hits == 1 else "fail"
    logger.info("verdict=%s bear=%s bull=%s", verdict, t_bear, t_bull)

    def _row(tag, d, want):
        p = d.get("p")
        return (
            f"| {tag} | {d['n']} | "
            + (f"{d['mean'] * 100:+.2f}%" if d["mean"] is not None else "—")
            + f" | {np.mean(baseline) * 100:+.2f}% | "
            + (f"{p:.4f}" if p is not None else "—")
            + f" | 期望{want} |"
        )

    lines = [
        "# ICT-8 SMT Divergence 实证 v1(BTC/ETH 4H,预注册)",
        "",
        "- 生成:`python -m sel_v2.offline.smt_study`(整文件覆写)",
        f"- 数据:v2_bars_4h BTC⋈ETH 对齐 {n} bars,{times[0]:%Y-%m-%d} → {times[-1]:%Y-%m-%d}"
        f"(ETH 深史 2026-07-12 经 binance_backfill 回填)",
        f"- BTC/ETH 4H 收益相关性:**{corr:.3f}**(高相关是 SMT 语义的前提——背离必须是异常)",
        f"- 预注册:双资产 1.5×ATR zigzag;枢轴配对窗 ±{MATCH_WINDOW} bar;事件=双方确认后;"
        "检验 bear→BTC 前向<基准 / bull→>;pass=双方向 p<0.10,单=partial",
        "",
        f"## 事件:{len(events)}(bear {len(bear)} / bull {len(bull)})",
        "",
        "| 方向 | n | 事件前向均值(BTC 6-bar) | 基准均值 | MW p | 备注 |",
        "|---|---:|---:|---:|---:|---|",
        _row("bearish SMT(背离高点)", t_bear, "<"),
        _row("bullish SMT(背离低点)", t_bull, ">"),
        "",
        f"## verdict:**{verdict}**",
        "",
    ]
    report = "\n".join(lines)
    try:
        REPORT_PATH.write_text(report)
        logger.info("report written: %s", REPORT_PATH)
    except OSError as exc:
        logger.warning("could not write report: %s", exc)
    print(report)
    return {"verdict": verdict, "n_events": len(events), "corr": corr}


if __name__ == "__main__":
    asyncio.run(run())
