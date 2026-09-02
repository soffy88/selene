"""正统缠论技术实证 (2026-07-11 batch) — CHAN-4 分型/笔分割 vs zigzag 代理,
CHAN-5 MACD 面积背驰 vs CHAN-2 动量比背驰.

用户指令("实证缠论的最新技术,看看哪些有必要加进来")的落地:2024-2026 缠论
"最新技术"扫描(czsc / chan.py 两大开源框架 + 社区)后,可机械化且未入池的真空白
只有两个,都在与既有 lens 研究完全相同的 2yr 数据上做**受控对照**:

  CHAN-4: 正统分型/笔/中枢分割(K线包含合并 + 新笔规则)是否优于我们一直用的
          1.5xATR zigzag 代理 —— 同一批假设(腿方向一致率 / 中枢重叠的前向波动
          分离)双分割各跑一遍,直接比。
  CHAN-5: 经典 MACD 面积背驰(因果化为面积率)是否优于 CHAN-2 的动量比背驰 ——
          事件结构/阈值完全相同,只换度量。

    python -m sel_v2.offline.chan_orthodox_study

复用 lens_study 的加载与假设检验函数(同一代码路径 → 同一统计口径)。
Offline-only;不碰 strategies/** / states/** / epoch。报告确定性(seed=42,整文件覆写)。
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import asyncpg
import numpy as np

from sel_v2.offline.chan_lens import detect_divergences, pivot_overlap_series
from sel_v2.offline.chan_orthodox import (
    build_strokes,
    detect_fractals,
    detect_macd_divergences,
    macd_histogram,
    merge_inclusion,
    stroke_direction_series,
    stroke_overlap_series,
)
from sel_v2.offline.ict_lens import structure_series
from sel_v2.offline.lens_common import (
    atr_zigzag_swings,
    bh_adjust,
    compute_atr,
    fisher_one_sided,
    surging_legs,
)
from sel_v2.offline.lens_study import (
    END_SOON_BARS,
    FP_WINDOW_BARS,
    MIN_GROUP_N,
    _load_joined,
    test_h_chan3,
    test_h_chan3b,
    test_h_ict2a,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("chan_orthodox_study")

REPORT_PATH = Path(__file__).resolve().parents[2] / "analysis" / "chan_orthodox_v1.md"


# ── shared evaluators (candidate-bar sets → same 2x2 / lead / FP stats) ───────


def _divergence_2x2(legs, cand_bars: set[int], testable: list[int]) -> dict:
    legs_by_id = {leg.leg_id: leg for leg in legs}
    tbl = [[0, 0], [0, 0]]
    for lid in testable:
        leg = legs_by_id[lid]
        for i in range(leg.start_idx + 1, leg.end_idx + 1):
            soon = i >= leg.end_idx - (END_SOON_BARS - 1)
            tbl[0 if i in cand_bars else 1][0 if soon else 1] += 1
    n_cand = tbl[0][0] + tbl[0][1]
    res = {"table": tbl, "n_candidates": n_cand, "n_testable": len(testable)}
    if n_cand >= MIN_GROUP_N:
        p = fisher_one_sided(tbl)
        res.update({"p": p, "verdict": "pass" if p < 0.10 else "fail"})
    else:
        res["verdict"] = "UNDERPOWERED"
    return res


def _lead_fp(legs, cand_by_leg: dict[int, list[int]]) -> dict:
    """Per-Exhaustion-leg last-candidate lead + Surging-wide FP rate."""
    leads = []
    hits = misses = 0
    for leg in legs:
        sigs = cand_by_leg.get(leg.leg_id, [])
        for i in sigs:
            if leg.end_idx + 1 - i <= FP_WINDOW_BARS:
                hits += 1
            else:
                misses += 1
        if leg.end_via == "Exhaustion" and sigs:
            leads.append(leg.end_idx + 1 - sigs[-1])
    return {
        "median_lead": float(np.median(leads)) if leads else None,
        "n_legs_signaled": len(leads),
        "fp": (hits, misses),
    }


# ── report ────────────────────────────────────────────────────────────────────


def _fmt_p(x) -> str:
    return f"{x:.4f}" if x is not None else "—"


async def run() -> dict:
    dsn = os.environ["DB_URL"].replace("postgresql+asyncpg://", "postgresql://")
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=3)
    try:
        times, high, low, close, _vol, states, via, _deg = await _load_joined(pool)
    finally:
        await pool.close()
    n = len(close)
    atr = compute_atr(high, low, close)
    legs = surging_legs(states, via, close)
    logger.info("loaded %d bars, %d legs", n, len(legs))

    # ── CHAN-4: orthodox segmentation ────────────────────────────────────────
    merged = merge_inclusion(high, low)
    strokes = build_strokes(merged, detect_fractals(merged))
    swings = atr_zigzag_swings(close, atr)
    stroke_lens = [abs(s.end_raw - s.start_raw) for s in strokes]
    census = {
        "merged_bars": len(merged),
        "n_strokes": len(strokes),
        "stroke_len_med": float(np.median(stroke_lens)) if stroke_lens else None,
        "n_zigzag_swings": len(swings),
    }
    logger.info("census: %s", census)

    # H-CHAN4a: leg-direction agreement, stroke structure vs zigzag structure
    stroke_states = stroke_direction_series(strokes, n)
    zz_states, _zz_events = structure_series(close, atr)
    agree_stroke = test_h_ict2a(states, via, close, stroke_states)
    agree_zz = test_h_ict2a(states, via, close, zz_states)

    # H-CHAN4b: orthodox pivot overlap vs zigzag pivot overlap (same tests)
    ov_stroke = stroke_overlap_series(strokes, n, atr)
    ov_zz = pivot_overlap_series(close, atr)
    sigma_dummy = np.full(n, np.nan)  # 3a lift domains: reuse calm/coiling only
    lift_stroke = test_h_chan3(states, ov_stroke, sigma_dummy)
    lift_zz = test_h_chan3(states, ov_zz, sigma_dummy)
    fwd_stroke = test_h_chan3b(states, ov_stroke, close)
    fwd_zz = test_h_chan3b(states, ov_zz, close)

    # ── CHAN-5: MACD-area divergence vs momentum-ratio divergence ────────────
    hist = macd_histogram(close)
    macd_cands, macd_testable = detect_macd_divergences(legs, close, hist)
    mom_cands, mom_testable = detect_divergences(legs, close)
    macd_2x2 = _divergence_2x2(legs, {c.bar_idx for c in macd_cands}, macd_testable)
    mom_2x2 = _divergence_2x2(legs, {c.bar_idx for c in mom_cands}, mom_testable)

    def _by_leg(cands):
        d: dict[int, list[int]] = {}
        for c in cands:
            d.setdefault(c.leg_id, []).append(c.bar_idx)
        return {k: sorted(v) for k, v in d.items()}

    macd_lf = _lead_fp(legs, _by_leg(macd_cands))
    mom_lf = _lead_fp(legs, _by_leg(mom_cands))

    # BH across this study's computed primary p-values
    family = []
    for tag, res in (
        ("H-CHAN4b[stroke]", fwd_stroke),
        ("H-CHAN4b[zigzag基线]", fwd_zz),
    ):
        if "p_vol" in res:
            family.append((tag, res["p_vol"]))
    for tag, res in (("H-CHAN5[macd]", macd_2x2), ("H-CHAN5[momentum基线]", mom_2x2)):
        if "p" in res:
            family.append((tag, res["p"]))
    qs = bh_adjust([p for _t, p in family]) if family else []
    fam_q = {t: q for (t, _p), q in zip(family, qs, strict=False)}

    # ── verdicts ─────────────────────────────────────────────────────────────
    # CHAN-4 adds value only if it BEATS the zigzag baseline somewhere material
    a_s, n_s = agree_stroke["k"], agree_stroke["n"]
    a_z, n_z = agree_zz["k"], agree_zz["n"]
    chan4_wins = []
    if a_s > a_z:
        chan4_wins.append(f"腿方向一致率 {a_s}/{n_s} > 基线 {a_z}/{n_z}")
    if "p_vol" in fwd_stroke and "p_vol" in fwd_zz and fwd_stroke["p_vol"] < fwd_zz["p_vol"]:
        chan4_wins.append("中枢重叠前向波动分离更强")
    chan4_verdict = "GO(优于代理)" if chan4_wins else "NO-GO(不优于既有 zigzag 代理)"

    chan5_better_lead = (
        macd_lf["median_lead"] is not None
        and mom_lf["median_lead"] is not None
        and macd_lf["median_lead"] > mom_lf["median_lead"]
    )
    mh, mm = macd_lf["fp"]
    oh, om = mom_lf["fp"]
    macd_fp_rate = mm / max(1, mh + mm)
    mom_fp_rate = om / max(1, oh + om)
    chan5_wins = []
    if macd_2x2.get("p", 1.0) < mom_2x2.get("p", 1.0):
        chan5_wins.append(f"2x2 更显著(p {_fmt_p(macd_2x2.get('p'))} < {_fmt_p(mom_2x2.get('p'))})")
    if macd_fp_rate < mom_fp_rate:
        chan5_wins.append(f"FP 率更低({macd_fp_rate:.0%} < {mom_fp_rate:.0%})")
    if chan5_better_lead:
        chan5_wins.append("lead 更早")
    chan5_verdict = (
        "GO(优于动量比)"
        if chan5_wins and macd_2x2.get("verdict") == "pass"
        else "NO-GO(不优于 CHAN-2 动量比)"
        if macd_2x2.get("verdict") != "UNDERPOWERED"
        else "UNDERPOWERED"
    )

    lines = [
        "# 正统缠论技术实证 v1(CHAN-4 分割对照 + CHAN-5 MACD 面积背驰)",
        "",
        "- 生成:`python -m sel_v2.offline.chan_orthodox_study`(seed=42,整文件覆写)",
        f"- 数据:与 lens 研究完全相同的 `v2_bars_4h ⋈ v2_state_annotation`,"
        f"{times[0]:%Y-%m-%d} → {times[-1]:%Y-%m-%d},{n} bars,Surging 腿 {len(legs)} 条",
        '- 来源:2024-2026 缠论"最新技术"扫描(czsc、chan.py、社区;学术零同行评审)。',
        "  可机械化真空白仅二:正统分型/笔分割、MACD 面积背驰——均为**受控对照**"
        "(同假设/同事件结构,只换分割或度量),不看结果挑参。",
        "",
        "## 分割普查",
        "",
        f"- K线包含合并:{n} 根原始 bar → {census['merged_bars']} 根合并 bar",
        f"- 笔:**{census['n_strokes']}** 条(中位长度 {census['stroke_len_med']:.0f} 根原始 bar);"
        f"zigzag(1.5×ATR)swing:**{census['n_zigzag_swings']}** 段 —— 笔的分辨率更细",
        "",
        "## H-CHAN4a 腿方向一致率(笔结构 vs zigzag 结构,同 H-ICT2a 口径)",
        "",
        "| 分割 | 一致 | CP 95% CI | 剔除 RANGE |",
        "|---|---|---|---|",
        f"| 笔(正统) | **{a_s}/{n_s}** | [{agree_stroke['ci'][0] * 100:.0f}%, {agree_stroke['ci'][1] * 100:.0f}%] "
        f"| {agree_stroke['k_range_excl']}/{agree_stroke['n_range_excl']} |",
        f"| zigzag 基线 | {a_z}/{n_z} | [{agree_zz['ci'][0] * 100:.0f}%, {agree_zz['ci'][1] * 100:.0f}%] "
        f"| {agree_zz['k_range_excl']}/{agree_zz['n_range_excl']} |",
        "",
        "## H-CHAN4b 中枢重叠 → 前向 6-bar 波动分离(同 H-CHAN3b 口径)",
        "",
        "| 分割 | MW p(波动) | X median | 对照 median | verdict |",
        "|---|---:|---:|---:|---|",
    ]
    for tag, res in (("笔(正统)", fwd_stroke), ("zigzag 基线", fwd_zz)):
        if "p_vol" in res:
            lines.append(
                f"| {tag} | {res['p_vol']:.4f} | {res['median_x']:.5f} | {res['median_ctrl']:.5f} | {res['verdict']} |"
            )
        else:
            lines.append(f"| {tag} | — | — | — | {res['verdict']} |")
    lines += [
        "",
        "附:Calm-lift 对照(同 H-CHAN3a 口径)——笔:p="
        f"{_fmt_p(lift_stroke['calm']['p'])},zigzag:p={_fmt_p(lift_zz['calm']['p'])}",
        "",
        "## H-CHAN5 MACD 面积背驰 vs CHAN-2 动量比背驰(同腿/同触发/同 0.7 阈,只换度量)",
        "",
        '> 因果化说明:经典"等 C 段走完比面积"不可因果计算;此处用**面积率**'
        "(|MACD柱|累计/已历 bar 数)对齐 CHAN-2 的动量率口径。",
        "",
        "| 度量 | 候选 bar | 2×2 Fisher p | 末段 lead(中位) | FP 率 | verdict |",
        "|---|---:|---:|---:|---:|---|",
        f"| MACD 面积率 | {macd_2x2['n_candidates']} | {_fmt_p(macd_2x2.get('p'))} "
        f"| {macd_lf['median_lead'] if macd_lf['median_lead'] is not None else '—'} "
        f"| {macd_fp_rate:.0%} | {macd_2x2['verdict']} |",
        f"| 动量比(CHAN-2 基线) | {mom_2x2['n_candidates']} | {_fmt_p(mom_2x2.get('p'))} "
        f"| {mom_lf['median_lead'] if mom_lf['median_lead'] is not None else '—'} "
        f"| {mom_fp_rate:.0%} | {mom_2x2['verdict']} |",
        "",
        "## BH 校正(本研究族)",
        "",
        "| 检验 | 裸 p | q |",
        "|---|---:|---:|",
    ]
    for tag, p in family:
        lines.append(f"| {tag} | {p:.4f} | {fam_q[tag]:.4f} |")
    lines += [
        "",
        "## 裁决与入池建议",
        "",
        f"- **CHAN-4(正统分型/笔分割):{chan4_verdict}**" + (f" —— {'; '.join(chan4_wins)}" if chan4_wins else ""),
        f"- **CHAN-5(MACD 面积背驰):{chan5_verdict}**" + (f" —— {'; '.join(chan5_wins)}" if chan5_wins else ""),
        "",
        '### 扫描中排除的"最新技术"(记录,不实证)',
        "",
        "- **chan.py 的 ML 买卖点管线**(500+特征 + XGB/LGBM 标签):4H BTC 事件量"
        "(58 突破 / 13 腿)差 2-3 个数量级,不可行;Month-6+ 事件积累后可重议",
        "- **区间套 / segseg 级别递归 / 多级别联立**:维持拒收(违反 4H 单锚点公理)",
        "- **一/二类买卖点**:一买一卖维持拒收(抄底摸顶);二买依赖一买识别,连带",
        "- **三类买卖点变体**(chan.py divergence_rate/min_zs_cnt 参数族):CHAN-1 几何代理已明确 fail,不做变体挑参(纪律)",
        '- **社区"背驰+中枢 100% 准确"类宣称**:自指定义不可证伪,反面教材',
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
        "census": census,
        "chan4": chan4_verdict,
        "chan5": chan5_verdict,
        "agree": (f"{a_s}/{n_s}", f"{a_z}/{n_z}"),
    }


if __name__ == "__main__":
    asyncio.run(run())
