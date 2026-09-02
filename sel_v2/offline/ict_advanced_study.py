"""ICT 最新技术实证 (2026-07-11 batch) — ICT-3 Killzones / ICT-4 Sweep /
ICT-5 FVG / ICT-6 Order Block,全部预注册参数,同一 2yr 数据.

用户指令("实证 ICT 最新技术,找到优秀方法加进来")的落地。2024-2026 扫描:SMC
机械概念(OB/FVG/sweep)零同行评审(零售宣称"FVG 70% 回补"、"Silver Bullet 70-80%
胜率"均未验证);学术上真正有据的是清算级联/stop-hunt(SSRN 2025-10 级联解剖等),
但自有清算数据 2026-07-06 起仅数日 —— 清算-sweep 关联标 PENDING(与 VPIN 同期
≈2026-08-05 补跑)。可立即实证的四个核心概念在此:

  H-ICT3 Killzones:   4H 时段的 |收益|/量 季节性(KW 检验 + 预注册稳健性:按日期
                      对半分,最高|收益|时段两半一致才算 pass)
  H-ICT4 Sweep:       假突破反转 — 分方向 MW 对基准(up:前向收益 < 基准;down:>)
                      **pass = 双方向均 p<0.10;单方向 = partial**
  H-ICT5 FVG:         回补率仅描述(无干净 null:价格终会回访多数价位);检验 =
                      首次触及后的方向支撑(分方向 MW 对基准,同 pass 规则)
  H-ICT6 Order Block: 位移+破结构后的区域回访反应(分方向 MW,同 pass 规则)

    python -m sel_v2.offline.ict_advanced_study

复用 lens_study._load_joined(同一代码路径/统计口径)。Offline-only;不碰
strategies/** / states/** / epoch。报告确定性(整文件覆写)。
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import asyncpg
import numpy as np

from sel_v2.offline.ict_advanced import (
    FWD_BARS,
    detect_fvgs,
    detect_order_blocks,
    detect_sweeps,
    fwd_return,
    slot_stats,
)
from sel_v2.offline.lens_common import bh_adjust, compute_atr, mann_whitney_one_sided
from sel_v2.offline.lens_study import MIN_GROUP_N, _load_joined

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("ict_advanced_study")

REPORT_PATH = Path(__file__).resolve().parents[2] / "analysis" / "ict_advanced_v1.md"
SLOTS = (0, 4, 8, 12, 16, 20)


def _fwd_all(close) -> np.ndarray:
    out = np.full(len(close), np.nan)
    for i in range(len(close)):
        v = fwd_return(close, i)
        if v is not None:
            out[i] = v
    return out


def _dir_test(event_fwd: list[float], baseline: np.ndarray, want: str) -> dict:
    """One direction's MW vs baseline. want='greater' → events above baseline."""
    ev = [x for x in event_fwd if x is not None and np.isfinite(x)]
    base = baseline[np.isfinite(baseline)]
    res = {
        "n": len(ev),
        "mean": float(np.mean(ev)) if ev else None,
        "base_mean": float(np.mean(base)),
    }
    if len(ev) >= MIN_GROUP_N:
        _u, p = mann_whitney_one_sided(ev, base, alternative=want)
        res["p"] = p
    return res


def _verdict(up: dict, down: dict) -> str:
    ps = [d.get("p") for d in (up, down)]
    if any(p is None for p in ps):
        return "UNDERPOWERED"
    hits = sum(1 for p in ps if p < 0.10)
    return "pass" if hits == 2 else "partial" if hits == 1 else "fail"


async def run() -> dict:
    dsn = os.environ["DB_URL"].replace("postgresql+asyncpg://", "postgresql://")
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=3)
    try:
        times, high, low, close, volume, states, via, _deg = await _load_joined(pool)
        lob_rows = await pool.fetch(
            "SELECT EXTRACT(hour FROM timestamp)::int AS hr, "
            "percentile_disc(0.5) WITHIN GROUP (ORDER BY bid_depth + ask_depth) AS med_depth "
            "FROM v2_lob_snapshots WHERE bid_depth IS NOT NULL GROUP BY 1 ORDER BY 1"
        )
        liq_rows = await pool.fetch(
            "SELECT to_timestamp(floor(EXTRACT(epoch FROM timestamp) / 14400) * 14400) "
            "AT TIME ZONE 'UTC' AS bar_ts, sum(size * price) AS notional "
            "FROM v2_liquidations WHERE symbol=$1 GROUP BY 1",
            "BTC-USDT",
        )
    finally:
        await pool.close()
    n = len(close)
    atr = compute_atr(high, low, close)
    baseline = _fwd_all(close)
    logger.info("loaded %d bars", n)

    # ── H-ICT3 killzones ─────────────────────────────────────────────────────
    from scipy.stats import kruskal

    st = slot_stats(times, close, volume)
    kw_ret = kruskal(*[st[s]["absret"] for s in SLOTS])
    kw_vol = kruskal(*[st[s]["volume"] for s in SLOTS])
    med_by_slot = {s: float(np.median(st[s]["absret"])) for s in SLOTS}
    top_slot = max(med_by_slot, key=med_by_slot.get)
    # preregistered robustness: date halves must agree on the top-|ret| slot
    half = times[len(times) // 2]
    tops = []
    for lohi in (lambda t: t < half, lambda t: t >= half):
        idx = [i for i, t in enumerate(times) if lohi(t)]
        sub_times = [times[i] for i in idx]
        sub = slot_stats(sub_times, close[idx], volume[idx])
        meds = {s: float(np.median(sub[s]["absret"])) if sub[s]["n"] else -1 for s in SLOTS}
        tops.append(max(meds, key=meds.get))
    kz_robust = tops[0] == tops[1] == top_slot
    kz_verdict = "pass" if (kw_ret.pvalue < 0.10 and kz_robust) else "fail"
    logger.info("killzones: KW p=%.2g top=%02d robust=%s", kw_ret.pvalue, top_slot, kz_robust)

    # ── H-ICT4 sweep ─────────────────────────────────────────────────────────
    sweeps = detect_sweeps(high, low, close)
    sw_up = _dir_test([fwd_return(close, e.idx) for e in sweeps if e.direction == 1], baseline, "less")
    sw_dn = _dir_test(
        [fwd_return(close, e.idx) for e in sweeps if e.direction == -1],
        baseline,
        "greater",
    )
    sw_verdict = _verdict(sw_up, sw_dn)
    logger.info("sweeps: %d events, verdict=%s", len(sweeps), sw_verdict)

    # ── H-ICT5 FVG ───────────────────────────────────────────────────────────
    fvgs = detect_fvgs(high, low, atr)
    bull = [e for e in fvgs if e.direction == 1]
    bear = [e for e in fvgs if e.direction == -1]
    fill_rate = sum(1 for e in fvgs if e.filled_at is not None) / max(1, len(fvgs))
    touch_rate = sum(1 for e in fvgs if e.touched_at is not None) / max(1, len(fvgs))
    fv_up = _dir_test(
        [fwd_return(close, e.touched_at) for e in bull if e.touched_at is not None],
        baseline,
        "greater",
    )
    fv_dn = _dir_test(
        [fwd_return(close, e.touched_at) for e in bear if e.touched_at is not None],
        baseline,
        "less",
    )
    fv_verdict = _verdict(fv_up, fv_dn)
    logger.info(
        "fvg: %d events (fill %.0f%%), verdict=%s",
        len(fvgs),
        fill_rate * 100,
        fv_verdict,
    )

    # ── H-ICT6 order block ───────────────────────────────────────────────────
    obs = detect_order_blocks(high, low, close, atr)
    ob_up = _dir_test(
        [fwd_return(close, e.revisit_idx) for e in obs if e.direction == 1 and e.revisit_idx],
        baseline,
        "greater",
    )
    ob_dn = _dir_test(
        [fwd_return(close, e.revisit_idx) for e in obs if e.direction == -1 and e.revisit_idx],
        baseline,
        "less",
    )
    ob_verdict = _verdict(ob_up, ob_dn)
    revisit_rate = sum(1 for e in obs if e.revisit_idx) / max(1, len(obs))
    logger.info(
        "ob: %d events (revisit %.0f%%), verdict=%s",
        len(obs),
        revisit_rate * 100,
        ob_verdict,
    )

    # ── liquidation-sweep (PREREGISTERED 2026-07-12; academically grounded angle) ──
    # sweep events should coincide with elevated liquidation notional in the
    # +-2 bar window vs all-bar baseline (MW one-sided greater). GUARD: <30d of
    # liquidation history -> PENDING (same clock as VPIN, ~2026-08-05).
    liq_sweep = {"status": "PENDING", "reason": "no liquidation data"}
    if liq_rows:
        import datetime as _dt

        liq_by_ts = {}
        for r in liq_rows:
            ts = r["bar_ts"]
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=_dt.timezone.utc)
            liq_by_ts[ts] = float(r["notional"])
        liq_span = (max(liq_by_ts) - min(liq_by_ts)).total_seconds() / 86400.0
        if liq_span < 30:
            liq_sweep = {
                "status": "PENDING",
                "reason": f"liquidation history {liq_span:.1f}d < 30d",
            }
        else:
            liq_series = np.array([liq_by_ts.get(t, 0.0) for t in times])
            liq_start = min(liq_by_ts)
            era = [i for i, t in enumerate(times) if t >= liq_start]
            sweep_idx = {e.idx for e in sweeps}

            def _win(i):
                return float(np.sum(liq_series[max(0, i - 2) : i + 3]))

            ev = [_win(i) for i in era if i in sweep_idx]
            base = [_win(i) for i in era if i not in sweep_idx]
            if len(ev) >= MIN_GROUP_N:
                _u, p_liq = mann_whitney_one_sided(ev, base, alternative="greater")
                liq_sweep = {
                    "status": "pass" if p_liq < 0.10 else "fail",
                    "p": p_liq,
                    "n_events": len(ev),
                    "med_event": float(np.median(ev)),
                    "med_base": float(np.median(base)),
                    "span_days": round(liq_span, 1),
                }
            else:
                liq_sweep = {"status": "UNDERPOWERED", "n_events": len(ev)}
    logger.info("liq-sweep: %s", liq_sweep.get("status"))

    # ── BH family ────────────────────────────────────────────────────────────
    family = [("H-ICT3[KW|ret|]", float(kw_ret.pvalue))]
    if "p" in liq_sweep:
        family.append(("H-ICT7[liq-sweep]", liq_sweep["p"]))
    for tag, d in (
        ("H-ICT4[sweep-up]", sw_up),
        ("H-ICT4[sweep-down]", sw_dn),
        ("H-ICT5[bull-touch]", fv_up),
        ("H-ICT5[bear-touch]", fv_dn),
        ("H-ICT6[bull-revisit]", ob_up),
        ("H-ICT6[bear-revisit]", ob_dn),
    ):
        if "p" in d:
            family.append((tag, d["p"]))
    qs = bh_adjust([p for _t, p in family])
    fam_q = dict(zip([t for t, _ in family], qs, strict=False))

    def _row(tag, d):
        p = d.get("p")
        return f"| {tag} | {d['n']} | {d['mean'] * 100:+.2f}% | {d['base_mean'] * 100:+.2f}% | " + (
            f"{p:.4f} | {fam_q.get(tag, float('nan')):.4f} |" if p is not None else "— | — |"
        )

    lines = [
        "# ICT 最新技术实证 v1(Killzones / Sweep / FVG / Order Block,预注册)",
        "",
        "- 生成:`python -m sel_v2.offline.ict_advanced_study`(整文件覆写)",
        f"- 数据:`v2_bars_4h ⋈ v2_state_annotation`,{times[0]:%Y-%m-%d} → {times[-1]:%Y-%m-%d},{n} bars",
        "- 扫描结论:SMC 机械概念零同行评审;清算-sweep 学术有据但自有清算数据仅数日"
        "(**PENDING ≈2026-08-05 与 VPIN 同期补跑**)。四候选参数全部预注册单组,不挑参。",
        f"- 判定规则(预注册):方向对称候选 pass=双方向 p<0.10,单方向=partial;"
        f"Killzones pass=KW p<0.10 且两个日期半样本最高|收益|时段一致。前向窗 {FWD_BARS} bar。",
        "",
        "## H-ICT3 Killzones(4H 时段季节性)",
        "",
        "| 时段(UTC) | n | median\\|ret\\| | median 量 |",
        "|---|---:|---:|---:|",
    ]
    for s in SLOTS:
        lines.append(
            f"| {s:02d}:00 | {st[s]['n']} | {np.median(st[s]['absret']) * 100:.3f}% "
            f"| {np.median(st[s]['volume']):,.0f} |"
        )
    lines += [
        "",
        f"- KW p(|收益|)= **{kw_ret.pvalue:.2e}**;KW p(量)= {kw_vol.pvalue:.2e}",
        f"- 最高|收益|时段:**{top_slot:02d}:00 UTC**;对半稳健性(两半最高时段一致):"
        f"**{'✓' if kz_robust else '✗ ' + '/'.join(f'{t:02d}' for t in tops)}**",
        "- LOB 时段深度(仅 "
        + f"{len(lob_rows)}"
        + " 小时桶 × ~8 天,**描述性**):"
        + (
            "峰值 "
            + max(lob_rows, key=lambda r: r["med_depth"])["hr"].__format__("02d")
            + ":00 / 谷值 "
            + min(lob_rows, key=lambda r: r["med_depth"])["hr"].__format__("02d")
            + ":00"
            if lob_rows
            else "无数据"
        ),
        f"- **verdict:{kz_verdict}**",
        "",
        "## H-ICT4 流动性 Sweep(假突破反转,lookback 30 bar)",
        "",
        f"- 事件:{len(sweeps)}(up {sum(1 for e in sweeps if e.direction == 1)} / "
        f"down {sum(1 for e in sweeps if e.direction == -1)})",
        "",
        "| 方向 | n | 事件前向均值 | 基准均值 | MW p | BH q |",
        "|---|---:|---:|---:|---:|---:|",
        _row("H-ICT4[sweep-up]", sw_up),
        _row("H-ICT4[sweep-down]", sw_dn),
        "",
        f"- **verdict:{sw_verdict}**(sweep-up 期望前向<基准,down 镜像)",
        "",
        "## H-ICT5 FVG(3-bar 缺口 ≥0.1×ATR)",
        "",
        f"- 事件:{len(fvgs)}(bull {len(bull)} / bear {len(bear)});"
        f"30-bar 内完全回补率 **{fill_rate:.0%}**、触及率 {touch_rate:.0%}"
        "(零售宣称 ~70%;**回补率无干净 null——价格终会回访多数价位,仅作描述**)",
        "",
        "| 方向 | n(首触) | 事件前向均值 | 基准均值 | MW p | BH q |",
        "|---|---:|---:|---:|---:|---:|",
        _row("H-ICT5[bull-touch]", fv_up),
        _row("H-ICT5[bear-touch]", fv_dn),
        "",
        f"- **verdict:{fv_verdict}**(检验 = 首触后的方向支撑,非回补率)",
        "",
        "## H-ICT6 Order Block(位移 2×ATR/≤3bar + 破 10-bar 结构,回访窗 60)",
        "",
        f"- 事件:{len(obs)};回访率 {revisit_rate:.0%}",
        "",
        "| 方向 | n(回访) | 事件前向均值 | 基准均值 | MW p | BH q |",
        "|---|---:|---:|---:|---:|---:|",
        _row("H-ICT6[bull-revisit]", ob_up),
        _row("H-ICT6[bear-revisit]", ob_dn),
        "",
        f"- **verdict:{ob_verdict}**",
        "",
        "## 汇总",
        "",
        "| 候选 | verdict |",
        "|---|---|",
        f"| ICT-3 Killzones | {kz_verdict} |",
        f"| ICT-4 Sweep | {sw_verdict} |",
        f"| ICT-5 FVG | {fv_verdict} |",
        f"| ICT-6 Order Block | {ob_verdict} |",
        (
            f"| ICT-7 清算-sweep 关联 | **{liq_sweep['status']}**"
            + (
                f"(p={liq_sweep['p']:.4f},事件窗清算中位 {liq_sweep['med_event']:,.0f} vs 基准 {liq_sweep['med_base']:,.0f},{liq_sweep['span_days']}d)|"
                if "p" in liq_sweep
                else f"({liq_sweep.get('reason', liq_sweep.get('n_events', ''))})|"
            )
        ),
        "",
    ]
    report = "\n".join(lines)
    try:
        REPORT_PATH.write_text(report)
        logger.info("report written: %s", REPORT_PATH)
    except OSError as exc:
        logger.warning("could not write report: %s", exc)
    print(report)
    return {
        "killzones": kz_verdict,
        "sweep": sw_verdict,
        "liq_sweep": liq_sweep.get("status"),
        "fvg": fv_verdict,
        "ob": ob_verdict,
        "counts": {"sweeps": len(sweeps), "fvgs": len(fvgs), "obs": len(obs)},
    }


if __name__ == "__main__":
    asyncio.run(run())
