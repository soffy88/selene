"""Chan / ICT lens empirical study (v2.2 candidate batch — offline, observation-only).

Three independent perspectives over the SAME joined 2yr BTC 4H data
(v2_bars_4h ⋈ v2_state_annotation):

  sel   — the frozen state machine's own annotation (ground truth stream)
  Chan  — CHAN-1 breakout-retest proxy / CHAN-2 divergence / CHAN-3 pivot overlap
  ICT   — ICT-2 swing structure (BOS/CHoCH) / ICT-1 VPIN pilot over v2_ticks

Each lens gets its own self-contained report; a fourth report holds the
hypothesis verdicts + the live-wiring go/no-go list and FROZEN live parameters
(single source of truth for the observation tools).

    python -m sel_v2.offline.lens_study              # full run (bars + ticks)
    python -m sel_v2.offline.lens_study --skip-ticks # bar lenses only

Pure counting/observation — no strategy parameters, no entry/exit gates, no P&L.
Never imported by any live decision path; touches nothing under strategies/**,
states/**, or the epoch. Reports are deterministic (seeded bootstraps, full-file
overwrite, data-coverage header).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path
from typing import Optional

import asyncpg
import numpy as np

from sel_v2.offline.chan_lens import (
    CONSOLIDATION_CONFIGS,
    RETEST_WINDOW_BARS,
    classify_retests,
    detect_breakouts,
    detect_divergences,
    pivot_overlap_series,
    sigma_pctile_series,
)
from sel_v2.offline.ict_lens import structure_series, vpin_pilot_stats
from sel_v2.offline.lens_common import (
    bh_adjust,
    bootstrap_mean_diff_ci,
    clopper_pearson,
    compute_atr,
    fisher_one_sided,
    mann_whitney_one_sided,
    surging_legs,
    welch_t_one_sided,
)
from sel_v2.observation_tools.vpin import VPINCalculator

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("lens_study")

SYMBOL = os.environ.get("SYMBOLS", "BTC-USDT")
ANALYSIS_DIR = Path(__file__).resolve().parents[2] / "analysis"

MIN_GROUP_N = 5  # below this a test is UNDERPOWERED, not failed
END_SOON_BARS = 3  # H-CHAN2: leg end within 3 bars
FP_WINDOW_BARS = 6  # H-ICT2b: signal followed by leg end within 6 bars
CONSOLIDATION_LIKE = ("Drifting_Calm",)  # 0 Coiling bars — the adapted regime
TRENDING_STATES = ("Surging", "Drifting_Charged", "Critical")
TICK_BATCH = 500_000
VPIN_RERUN_NOTE = (
    "v2_ticks starts 2026-07-06 (no retention drop) — first honest 30d window "
    "≈ 2026-08-05; re-run `python -m sel_v2.offline.lens_study` then."
)


# ── data loading ─────────────────────────────────────────────────────────────


async def _load_joined(pool):
    """bars ⋈ annotation on exact timestamps (leg_census._load pattern, plus
    volume / transition_via / degraded)."""
    bar_rows = await pool.fetch(
        "SELECT time, high, low, close, volume FROM v2_bars_4h "
        "WHERE symbol=$1 ORDER BY time ASC",
        SYMBOL,
    )
    ann_rows = await pool.fetch(
        "SELECT timestamp, state, transition_via, degraded FROM v2_state_annotation "
        "WHERE symbol=$1 ORDER BY timestamp ASC",
        SYMBOL,
    )
    ann = {r["timestamp"]: r for r in ann_rows}
    times, high, low, close, volume, states, via, degraded = (
        [],
        [],
        [],
        [],
        [],
        [],
        [],
        [],
    )
    for r in bar_rows:
        a = ann.get(r["time"])
        if a is None:
            continue
        times.append(r["time"])
        high.append(float(r["high"]))
        low.append(float(r["low"]))
        close.append(float(r["close"]))
        volume.append(float(r["volume"]))
        states.append(a["state"])
        via.append(a["transition_via"])
        degraded.append(bool(a["degraded"]))
    if not times:
        raise SystemExit("v2_bars_4h / v2_state_annotation join is empty — STOP")
    return (
        times,
        np.array(high),
        np.array(low),
        np.array(close),
        np.array(volume),
        states,
        via,
        degraded,
    )


async def _run_vpin_pilot(pool, times, volume) -> dict:
    """Stream v2_ticks through a VPINCalculator (keyset pagination — never
    materializes the full table). Returns pilot stats + bootstrap notes."""
    # V_bucket bootstrap: trailing 30d of 4H bar volume (both feeds are
    # Binance-derived via the iris layer; the tick/bar volume ratio below is a
    # sanity check, printed in the report)
    bars_30d = min(len(volume), 30 * 6)
    bar_vol_30d = float(np.sum(volume[-bars_30d:]))
    v_bucket0 = bar_vol_30d / 30.0 / 50.0

    row = await pool.fetchrow(
        "SELECT min(timestamp) AS lo, max(timestamp) AS hi, sum(size) AS vol "
        "FROM v2_ticks WHERE symbol=$1",
        SYMBOL,
    )
    if row is None or row["lo"] is None:
        return {"available": False}
    tick_lo, tick_hi, tick_vol = row["lo"], row["hi"], float(row["vol"])
    overlap_bar_vol = float(
        sum(v for t, v in zip(times, volume) if tick_lo <= t <= tick_hi)
    )
    ratio = tick_vol / overlap_bar_vol if overlap_bar_vol > 0 else float("nan")
    v_bucket = (
        v_bucket0 * ratio
        if np.isfinite(ratio) and not 0.5 <= ratio <= 2.0
        else v_bucket0
    )

    calc = VPINCalculator(v_bucket=v_bucket)
    series: list[tuple] = []
    durations: list[float] = []
    last_ts, last_id = tick_lo, ""
    n_ticks = 0
    while True:
        rows = await pool.fetch(
            "SELECT timestamp, price, size, side, trade_id FROM v2_ticks "
            "WHERE symbol=$1 AND (timestamp, trade_id) > ($2, $3) "
            "ORDER BY timestamp, trade_id LIMIT $4",
            SYMBOL,
            last_ts,
            last_id,
            TICK_BATCH,
        )
        if not rows:
            break
        for r in rows:
            for b in calc.on_tick(
                r["timestamp"], float(r["price"]), float(r["size"]), r["side"]
            ):
                series.append((b.close_ts, calc.vpin, calc.bvc_vpin))
                durations.append(b.duration_s / 60.0)
        last_ts, last_id = rows[-1]["timestamp"], rows[-1]["trade_id"]
        n_ticks += len(rows)
        logger.info("vpin pilot: %d ticks streamed, %d buckets", n_ticks, len(series))
    stats = vpin_pilot_stats(series, durations)
    stats.update(
        {
            "available": True,
            "n_ticks": n_ticks,
            "tick_span": (tick_lo, tick_hi),
            "v_bucket0": v_bucket0,
            "v_bucket": v_bucket,
            "tick_bar_volume_ratio": ratio,
            "p95": calc.percentile(95),
            "p97": calc.percentile(97),
            "warmed": calc.completed_buckets >= calc.warmup_buckets,
        }
    )
    return stats


# ── hypothesis tests ─────────────────────────────────────────────────────────


def _fwd_window_stats(close, i, bars=6):
    """(realized vol, |log return|) over the `bars` bars after bar i; None at tail."""
    if i + bars >= len(close):
        return None, None
    seg = np.log(close[i + 1 : i + bars + 1] / close[i : i + bars])
    return float(np.std(seg)), float(abs(np.log(close[i + bars] / close[i])))


def test_h_chan1(high, low, close, atr) -> dict:
    """Breakout-retest per config: A vs C forward return (one-sided, A > C)."""
    out = {}
    for name, min_bars, mult in CONSOLIDATION_CONFIGS:
        events = detect_breakouts(high, low, close, atr, min_bars, mult)
        outcomes = classify_retests(events, high, low, close)
        counts = {c: sum(1 for o in outcomes if o.retest_class == c) for c in "ABC"}
        a = [
            o.fwd_ret_24h
            for o in outcomes
            if o.retest_class == "A" and o.fwd_ret_24h is not None
        ]
        c = [
            o.fwd_ret_24h
            for o in outcomes
            if o.retest_class == "C" and o.fwd_ret_24h is not None
        ]
        truncated = sum(1 for o in outcomes if o.fwd_ret_24h is None)
        res = {
            "events": len(events),
            "outcomes": outcomes,
            "counts": counts,
            "n_a": len(a),
            "n_c": len(c),
            "truncated": truncated,
            "mean_a": float(np.mean(a)) if a else None,
            "mean_c": float(np.mean(c)) if c else None,
        }
        if min(len(a), len(c)) >= MIN_GROUP_N:
            t, p_t = welch_t_one_sided(a, c)
            _u, p_mw = mann_whitney_one_sided(a, c)
            ci = bootstrap_mean_diff_ci(a, c)
            res.update(
                {
                    "verdict": "pass" if p_t < 0.10 else "fail",
                    "t": t,
                    "p": p_t,
                    "p_mw": p_mw,
                    "ci90": ci,
                }
            )
        else:
            res["verdict"] = "UNDERPOWERED"
        out[name] = res
    return out


def test_h_chan2(states, via, close) -> dict:
    """Divergence candidates vs leg-end-within-3-bars (Fisher, one-sided)."""
    legs = surging_legs(states, via, close)
    cands, testable = detect_divergences(legs, close)
    cand_bars = {c.bar_idx for c in cands}
    legs_by_id = {l.leg_id: l for l in legs}
    rows = []  # (is_candidate, ends_soon) per bar of testable legs (entry bar skipped)
    for lid in testable:
        leg = legs_by_id[lid]
        for i in range(leg.start_idx + 1, leg.end_idx + 1):
            rows.append((i in cand_bars, i >= leg.end_idx - (END_SOON_BARS - 1)))
    tbl = [[0, 0], [0, 0]]
    for is_c, soon in rows:
        tbl[0 if is_c else 1][0 if soon else 1] += 1
    # leg-level descriptive view
    leg_view = []
    for lid in testable:
        leg = legs_by_id[lid]
        fired = sorted(c.bar_idx for c in cands if c.leg_id == lid)
        final3 = any(i >= leg.end_idx - (END_SOON_BARS - 1) for i in fired)
        leg_view.append((lid, len(fired), final3, bool(fired)))
    res = {
        "n_legs": len(legs),
        "n_testable": len(testable),
        "n_candidates": len(cands),
        "table": tbl,
        "leg_view": leg_view,
        "legs": legs,
        "candidates": cands,
    }
    if len(cands) >= MIN_GROUP_N:
        p = fisher_one_sided(tbl)
        res.update({"p": p, "verdict": "pass" if p < 0.10 else "fail"})
    else:
        res["verdict"] = "UNDERPOWERED"
    return res


def test_h_chan3(states, overlap, sigma_pct) -> dict:
    """3a (adapted): overlap>p70 lift vs Drifting_Calm and vs low-σ regimes.
    Coiling itself is UNTESTABLE (0 annotated bars)."""
    finite = np.isfinite(overlap)
    p70 = float(np.nanpercentile(overlap[finite], 70)) if finite.any() else float("nan")
    hi = finite & (overlap > p70)
    lo = finite & ~(overlap > p70)
    res = {"p70": p70, "n_finite": int(finite.sum()), "n_hi": int(hi.sum())}
    arr_states = np.array(states)
    for key, mask in (
        ("calm", np.isin(arr_states, CONSOLIDATION_LIKE)),
        ("low_sigma", np.isfinite(sigma_pct) & (sigma_pct < 0.30)),
    ):
        tbl = [
            [int((hi & mask).sum()), int((hi & ~mask).sum())],
            [int((lo & mask).sum()), int((lo & ~mask).sum())],
        ]
        rate_hi = tbl[0][0] / max(1, tbl[0][0] + tbl[0][1])
        rate_lo = tbl[1][0] / max(1, tbl[1][0] + tbl[1][1])
        res[key] = {
            "table": tbl,
            "rate_hi": rate_hi,
            "rate_lo": rate_lo,
            "p": fisher_one_sided(tbl),
        }
    res["verdict_3a"] = (
        "adapted-pass"
        if (res["calm"]["p"] < 0.10 or res["low_sigma"]["p"] < 0.10)
        else "adapted-fail"
    )
    return res


def test_h_chan3b(states, overlap, close) -> dict:
    """High-overlap bars inside trending sel states: do they behave like
    consolidation going forward (lower fwd 6-bar movement than same-state
    low-overlap controls)?"""
    finite = np.isfinite(overlap)
    p70 = float(np.nanpercentile(overlap[finite], 70)) if finite.any() else float("nan")
    arr_states = np.array(states)
    trending = np.isin(arr_states, TRENDING_STATES)
    calm = np.isin(arr_states, CONSOLIDATION_LIKE)

    def _collect(mask):
        vols, moves = [], []
        for i in np.where(mask)[0]:
            v, m = _fwd_window_stats(close, int(i))
            if v is not None:
                vols.append(v)
                moves.append(m)
        return vols, moves

    x_vols, x_moves = _collect(finite & (overlap > p70) & trending)
    a_vols, a_moves = _collect(finite & ~(overlap > p70) & trending)
    b_vols, _b_moves = _collect(calm)
    res = {"n_x": len(x_vols), "n_ctrl_same_state": len(a_vols), "n_calm": len(b_vols)}
    if min(len(x_vols), len(a_vols)) >= MIN_GROUP_N:
        _u, p_vol = mann_whitney_one_sided(a_vols, x_vols)  # X lower → a > x
        _u2, p_move = mann_whitney_one_sided(a_moves, x_moves)
        ci = bootstrap_mean_diff_ci(x_vols, a_vols)
        res.update(
            {
                "p_vol": p_vol,
                "p_move": p_move,
                "ci90_vol_diff": ci,
                "median_x": float(np.median(x_vols)),
                "median_ctrl": float(np.median(a_vols)),
                "median_calm": float(np.median(b_vols)) if b_vols else None,
                "verdict": "pass" if min(p_vol, p_move) < 0.10 else "fail",
            }
        )
    else:
        res["verdict"] = "UNDERPOWERED"
    return res


def test_h_ict2a(states, via, close, struct_states) -> dict:
    """Leg-level modal structure vs Surging leg direction; Clopper-Pearson CI.
    Two counting rules: RANGE-as-disagreement and RANGE-excluded."""
    legs = [l for l in surging_legs(states, via, close) if l.direction != 0]
    per_leg = []
    for leg in legs:
        seg = struct_states[leg.start_idx : leg.end_idx + 1]
        vals, counts = np.unique(seg, return_counts=True)
        modal = str(vals[np.argmax(counts)])
        want = "UP" if leg.direction == 1 else "DOWN"
        per_leg.append((leg, modal, modal == want))
    n = len(per_leg)
    k = sum(1 for _l, _m, ok in per_leg if ok)
    non_range = [(l, m, ok) for l, m, ok in per_leg if m != "RANGE"]
    k2, n2 = sum(1 for _l, _m, ok in non_range if ok), len(non_range)
    # bar-level (secondary, serially dependent)
    bar_n = bar_k = 0
    for leg in legs:
        want = "UP" if leg.direction == 1 else "DOWN"
        for i in range(leg.start_idx, leg.end_idx + 1):
            bar_n += 1
            bar_k += struct_states[i] == want
    return {
        "per_leg": per_leg,
        "n": n,
        "k": k,
        "ci": clopper_pearson(k, n) if n else (0.0, 1.0),
        "n_range_excl": n2,
        "k_range_excl": k2,
        "ci_range_excl": clopper_pearson(k2, n2) if n2 else (0.0, 1.0),
        "bar_rate": bar_k / bar_n if bar_n else None,
        "bar_n": bar_n,
    }


def test_h_ict2b(states, via, close, struct_events, chan2) -> dict:
    """CHoCH vs CHAN-2 divergence: lead time to Exhaustion leg ends + FP rates."""
    legs = [l for l in chan2["legs"] if l.end_via == "Exhaustion"]
    cands = chan2["candidates"]
    ev_by_idx = [(e.idx, e.kind) for e in struct_events]
    pairs, table_rows = [], []
    for leg in legs:
        counter = "CHOCH_DOWN" if leg.direction == 1 else "CHOCH_UP"
        choch_in = [
            i
            for i, k in ev_by_idx
            if k == counter and leg.start_idx <= i <= leg.end_idx
        ]
        div_in = [c.bar_idx for c in cands if c.leg_id == leg.leg_id]
        end_bar = leg.end_idx + 1  # the transition bar
        lead_c = end_bar - choch_in[-1] if choch_in else None
        lead_d = end_bar - div_in[-1] if div_in else None
        table_rows.append((leg.leg_id, leg.direction, lead_c, lead_d))
        if lead_c is not None and lead_d is not None:
            pairs.append((lead_c, lead_d))
    res = {"rows": table_rows, "n_exh_legs": len(legs), "n_pairs": len(pairs)}
    if len(pairs) >= 6:
        from scipy.stats import wilcoxon

        c_leads, d_leads = zip(*pairs)
        try:
            stat, p = wilcoxon(c_leads, d_leads)
            res.update({"wilcoxon_p": float(p)})
        except ValueError:  # all-zero differences
            res.update({"wilcoxon_p": None})
    if pairs:
        res["median_lead_choch"] = float(np.median([c for c, _d in pairs]))
        res["median_lead_div"] = float(np.median([d for _c, d in pairs]))
    # FP comparison: signal during ANY Surging leg not followed by leg end within 6 bars
    all_legs = chan2["legs"]

    def _fp(sig_indices_per_leg):
        hits = misses = 0
        for leg, sigs in sig_indices_per_leg:
            for i in sigs:
                if leg.end_idx + 1 - i <= FP_WINDOW_BARS:
                    hits += 1
                else:
                    misses += 1
        return hits, misses

    choch_sigs = []
    div_sigs = []
    for leg in all_legs:
        if leg.direction == 0:
            continue
        counter = "CHOCH_DOWN" if leg.direction == 1 else "CHOCH_UP"
        choch_sigs.append(
            (
                leg,
                [
                    i
                    for i, k in ev_by_idx
                    if k == counter and leg.start_idx <= i <= leg.end_idx
                ],
            )
        )
        div_sigs.append((leg, [c.bar_idx for c in cands if c.leg_id == leg.leg_id]))
    ch, cm = _fp(choch_sigs)
    dh, dm = _fp(div_sigs)
    res["fp_table"] = {"choch": (ch, cm), "div": (dh, dm)}
    if ch + cm >= MIN_GROUP_N and dh + dm >= MIN_GROUP_N:
        res["fp_fisher_p"] = fisher_one_sided([[ch, cm], [dh, dm]])
    return res


# ── report builders ──────────────────────────────────────────────────────────


def _hdr(title: str, times, n: int) -> list[str]:
    return [
        f"# {title}",
        "",
        f"- 生成:`python -m sel_v2.offline.lens_study`(确定性:seed=42,整文件覆写)",
        f"- 数据:`v2_bars_4h ⋈ v2_state_annotation`,{SYMBOL},"
        f"{times[0]:%Y-%m-%d} → {times[-1]:%Y-%m-%d},{n} bars(逐 bar 精确对齐)",
        "- 纪律:observation-only;不碰 `states/**`/`strategies/**`;三视角同数据可并排对比",
        "",
    ]


def _sample_banner() -> list[str]:
    return [
        "> **样本量诚实声明**:2 年标注中 Surging 腿仅 13 条(11 Exhaustion + 2 Stress 收尾),",
        "> Critical 16 bar,**Coiling=0、Cascade=0、Release=0**。凡涉及腿级/事件级检验均为小样本,",
        "> 功效有限;bar 级检验存在序列相依,p 值偏乐观(均已在对应小节标注)。",
        "",
    ]


def _build_sel_report(times, close, states, via, degraded, legs) -> str:
    n = len(states)
    from collections import Counter

    dist = Counter(states)
    lines = _hdr("sel 视角 v1 — 状态机自述(三视角对比基准)", times, n)
    lines += _sample_banner()
    lines += [
        "## 状态分布(2 年标注)",
        "",
        "| state | bars | share |",
        "|---|---:|---:|",
    ]
    for s, c in dist.most_common():
        lines.append(f"| {s} | {c} | {c / n * 100:.1f}% |")
    n_deg = sum(degraded)
    lines += [
        "",
        f"- degraded bars(特征缺失回退):{n_deg} / {n} = {n_deg / n * 100:.1f}%",
        "- **Coiling = 0、Cascade = 0**(历史特征降级,条件不可满足)、Release 转移 = 0",
        "",
        "## Surging 腿明细(13 条)",
        "",
        "| leg | start | end | bars | direction | net ret | end via |",
        "|---:|---|---|---:|---:|---:|---|",
    ]
    for leg in legs:
        ret = float(np.log(close[leg.end_idx] / close[leg.start_idx]))
        lines.append(
            f"| {leg.leg_id} | {times[leg.start_idx]:%Y-%m-%d} | {times[leg.end_idx]:%Y-%m-%d} "
            f"| {leg.end_idx - leg.start_idx + 1} | {'+1' if leg.direction == 1 else leg.direction} "
            f"| {ret * 100:+.1f}% | {leg.end_via or '—'} |"
        )
    dwell = [leg.end_idx - leg.start_idx + 1 for leg in legs]
    crit = [i for i, s in enumerate(states) if s == "Critical"]
    lines += [
        "",
        f"- 腿驻留(bars):median={np.median(dwell):.0f},min={min(dwell)},max={max(dwell)}"
        if dwell
        else "- 无腿",
        f"- Critical bars:{len(crit)}"
        + (
            "("
            + ", ".join(f"{times[i]:%Y-%m-%d}" for i in crit[:8])
            + (", …)" if len(crit) > 8 else ")")
            if crit
            else ""
        ),
        "",
        "## 现读(最近 30 bar)",
        "",
        f"- 状态序列:{' → '.join(_compress_states(states[-30:]))}",
        f"- 最新 bar:{times[-1]:%Y-%m-%d %H:%M} UTC,state={states[-1]},close={close[-1]:,.0f}",
        "",
    ]
    return "\n".join(lines)


def _compress_states(seq: list[str]) -> list[str]:
    out = []
    for s in seq:
        tag = {"Drifting_Calm": "Calm", "Drifting_Charged": "Charged"}.get(s, s)
        if not out or not out[-1].startswith(tag):
            out.append(f"{tag}×1")
        else:
            base, cnt = out[-1].rsplit("×", 1)
            out[-1] = f"{base}×{int(cnt) + 1}"
    return out


def _build_chan_report(times, close, chan1, chan2, chan3, chan3b, overlap) -> str:
    n = len(close)
    lines = _hdr("缠论视角 v1 — CHAN-1/2/3 实证", times, n)
    lines += [
        "> **CHAN-1 偏差横幅**:sel 的 Release(价格突破 + OFI 跃升 + OI 加速)2 年标注与 live",
        "> 均为 **0 次触发**,本节用**纯几何突破代理**(盘整区间收盘突破)实证 breakout-retest",
        "> 假设本身,**不等于**检验 sel Release 语义(用户裁决,候选池记录)。",
        '> 前向收益取 t+6→t+12(分类窗之后),与池原文"后续 24H"不同——避免分类窗与结果窗重叠的循环论证。',
        "",
        "## CHAN-1 盘整/突破/回踩普查(双参数组,都报,不挑参)",
        "",
    ]
    for name, res in chan1.items():
        c = res["counts"]
        lines += [
            f"### 参数组 {name}",
            "",
            f"- 突破事件:{res['events']}(A:{c['A']} B:{c['B']} C:{c['C']},尾部截断 {res['truncated']})",
            f"- A 类均值前向收益:{_fmtpct(res['mean_a'])};C 类:{_fmtpct(res['mean_c'])}",
        ]
        if res["verdict"] == "UNDERPOWERED":
            lines.append(
                f"- **UNDERPOWERED**(min(n_A={res['n_a']}, n_C={res['n_c']}) < {MIN_GROUP_N})——不下结论"
            )
        else:
            lo, hi = res["ci90"]
            lines += [
                f"- Welch 单侧 t={res['t']:.2f},p={res['p']:.4f};Mann-Whitney p={res['p_mw']:.4f}",
                f"- bootstrap 90% CI(mean_A − mean_C):[{lo * 100:+.2f}%, {hi * 100:+.2f}%](seed=42)",
                f"- **verdict({name}):{res['verdict']}**(α=0.10,池原文标准)",
            ]
        lines.append("")
    lines += [
        "## CHAN-2 背驰(标注 Surging 腿内)",
        "",
        f"- 腿总数 {chan2['n_legs']},可测腿(有同向前腿且前腿>1 bar){chan2['n_testable']},"
        f"背驰候选 bar {chan2['n_candidates']}",
        f"- 2×2(bar 级,序列相依 → p 偏乐观):{chan2['table']}",
    ]
    if "p" in chan2:
        lines.append(
            f"- Fisher 单侧 p={chan2['p']:.4f} → **verdict:{chan2['verdict']}**"
        )
    else:
        lines.append(f"- **{chan2['verdict']}**(候选 <{MIN_GROUP_N})")
    lines += [
        "",
        "- 腿级描述(不做检验,n 太小):",
        "",
        "| leg | 候选数 | 末 3 bar 内命中 |",
        "|---:|---:|---|",
    ]
    for lid, n_f, final3, _any in chan2["leg_view"]:
        lines.append(f"| {lid} | {n_f} | {'✓' if final3 else '✗'} |")
    lines += [
        "",
        "## CHAN-3 中枢重叠度",
        "",
        f"- overlap_ratio 有值 bar:{chan3['n_finite']}/{n};**p70 = {chan3['p70']:.3f}**(全样本内,live 冻结值)",
        "> **H-CHAN3a 原判据 UNTESTABLE**:标注中 Coiling = 0 bar。以下为改编检验(lift,",
        "> 非裸重合率——Drifting_Calm 基率 70.1%,裸重合率无意义):",
        "",
        "| 替代域 | 高 overlap 命中率 | 低 overlap 命中率 | Fisher p |",
        "|---|---:|---:|---:|",
    ]
    for key, label in (("calm", "Drifting_Calm"), ("low_sigma", "σ_pctile<30")):
        d = chan3[key]
        lines.append(
            f"| {label} | {d['rate_hi'] * 100:.1f}% | {d['rate_lo'] * 100:.1f}% | {d['p']:.4f} |"
        )
    lines += ["", f"- **verdict(3a,改编):{chan3['verdict_3a']}**", ""]
    lines += ["### H-CHAN3b 分歧样本(高 overlap 且 sel 处趋势态)", ""]
    if chan3b["verdict"] == "UNDERPOWERED":
        lines.append(
            f"- **UNDERPOWERED**(X={chan3b['n_x']},同态对照={chan3b['n_ctrl_same_state']})"
        )
    else:
        lines += [
            f"- X(n={chan3b['n_x']})前向 6-bar 波动 median={chan3b['median_x']:.5f};"
            f"同态对照(n={chan3b['n_ctrl_same_state']})={chan3b['median_ctrl']:.5f};"
            f"Calm 基准={chan3b['median_calm']:.5f}",
            f"- MW 单侧 p(波动)={chan3b['p_vol']:.4f};p(|收益|)={chan3b['p_move']:.4f};"
            f"CI90(X−对照)= [{chan3b['ci90_vol_diff'][0]:.5f}, {chan3b['ci90_vol_diff'][1]:.5f}]",
            f"- **verdict(3b):{chan3b['verdict']}**",
        ]
    hi_now = overlap[-1] if np.isfinite(overlap[-1]) else None
    lines += [
        "",
        "## 现读(最新 bar)",
        "",
        f"- overlap_ratio = {hi_now:.3f}"
        if hi_now is not None
        else "- overlap_ratio = n/a(swing 不足)",
        f"- 高于 p70?{'是' if hi_now is not None and hi_now > chan3['p70'] else '否/未知'}",
        "",
    ]
    return "\n".join(lines)


def _fmtpct(x: Optional[float]) -> str:
    return f"{x * 100:+.2f}%" if x is not None else "n/a"


def _build_ict_report(
    times, close, struct_states, struct_events, ict2a, ict2b, vpin
) -> str:
    n = len(close)
    from collections import Counter

    occ = Counter(struct_states)
    ev_c = Counter(e.kind for e in struct_events)
    lines = _hdr("ICT 视角 v1 — ICT-2 结构 + ICT-1 VPIN pilot", times, n)
    lines += _sample_banner()
    lines += [
        "## ICT-2 swing 结构(1.5×ATR zigzag,与 CHAN-3 共享;全部事件取确认时刻,无前视)",
        "",
        "| 结构态 | bars | share |",
        "|---|---:|---:|",
    ]
    for s in ("UP", "DOWN", "RANGE"):
        lines.append(f"| {s} | {occ.get(s, 0)} | {occ.get(s, 0) / n * 100:.1f}% |")
    lines += [
        "",
        f"- 事件普查:BOS_UP {ev_c.get('BOS_UP', 0)} / BOS_DOWN {ev_c.get('BOS_DOWN', 0)} / "
        f"CHOCH_UP {ev_c.get('CHOCH_UP', 0)} / CHOCH_DOWN {ev_c.get('CHOCH_DOWN', 0)}",
        "",
        "### H-ICT2a 结构方向 vs sel Surging 腿方向(腿方向=累计收益符号,sel Surging 无方向字段)",
        "",
        "| leg | 方向 | 众数结构态 | 一致 |",
        "|---:|---:|---|---|",
    ]
    for leg, modal, ok in ict2a["per_leg"]:
        lines.append(
            f"| {leg.leg_id} | {'+1' if leg.direction == 1 else '-1'} | {modal} | {'✓' if ok else '✗'} |"
        )
    lo, hi = ict2a["ci"]
    lo2, hi2 = ict2a["ci_range_excl"]
    lines += [
        "",
        f"- RANGE 计不一致:**{ict2a['k']}/{ict2a['n']} = "
        f"{ict2a['k'] / max(1, ict2a['n']) * 100:.0f}%**,CP 95% CI [{lo * 100:.0f}%, {hi * 100:.0f}%]",
        f"- 剔除 RANGE 众数腿:{ict2a['k_range_excl']}/{ict2a['n_range_excl']},"
        f"CI [{lo2 * 100:.0f}%, {hi2 * 100:.0f}%]",
        f"- bar 级(辅助,序列相依):{(ict2a['bar_rate'] or 0) * 100:.1f}%(n={ict2a['bar_n']})",
        f"- **n={ict2a['n']} 的 CI 宽 ±~25pp——>70% 只能以点估计评估,不作显著性声明**",
        "",
        "### H-ICT2b CHoCH vs CHAN-2 背驰:对 Exhaustion 腿终结的 lead(bar 数)",
        "",
        "| leg | 方向 | CHoCH lead | 背驰 lead |",
        "|---:|---:|---:|---:|",
    ]
    for lid, d, lc, ld in ict2b["rows"]:
        lines.append(
            f"| {lid} | {'+1' if d == 1 else '-1'} | {lc if lc is not None else '—'} "
            f"| {ld if ld is not None else '—'} |"
        )
    lines += [""]
    if "median_lead_choch" in ict2b:
        lines.append(
            f"- 配对腿 n={ict2b['n_pairs']}:median CHoCH lead = {ict2b['median_lead_choch']:.0f},"
            f"median 背驰 lead = {ict2b['median_lead_div']:.0f}"
            + (
                f";Wilcoxon p={ict2b['wilcoxon_p']:.3f}"
                if ict2b.get("wilcoxon_p") is not None
                else "(配对 <6 → descriptive only)"
            )
        )
    ch, cm = ict2b["fp_table"]["choch"]
    dh, dm = ict2b["fp_table"]["div"]
    lines += [
        f"- FP(Surging 内信号,{FP_WINDOW_BARS} bar 内未跟腿终结):CHoCH {cm}/{ch + cm},"
        f"背驰 {dm}/{dh + dm}"
        + (f";Fisher p={ict2b['fp_fisher_p']:.3f}" if "fp_fisher_p" in ict2b else ""),
        "",
        "## ICT-1 VPIN pilot(**H-ICT1a/1b:DATA-INSUFFICIENT-PENDING**)",
        "",
        f"> {VPIN_RERUN_NOTE}",
        "",
    ]
    if not vpin.get("available"):
        lines += ["- v2_ticks 不可用/为空 或本次 `--skip-ticks`——pilot 未跑", ""]
    else:
        d = vpin.get("distribution", {})
        lines += [
            f"- tick:{vpin['n_ticks']:,} 笔,{vpin['tick_span'][0]:%m-%d} → {vpin['tick_span'][1]:%m-%d};"
            f"完成桶 {vpin['n_buckets_total']},VPIN 点 {vpin['n_vpin_points']}",
            f"- V_bucket 引导:30d bar 量基线 → {vpin['v_bucket0']:,.1f}(coin 口径);"
            f"tick/bar 量比 = {vpin['tick_bar_volume_ratio']:.3f}"
            + (
                "(∈[0.5,2],同口径,不修正)"
                if 0.5 <= vpin["tick_bar_volume_ratio"] <= 2.0
                else "(∉[0.5,2] → tick size 与 bar volume 非同一单位,**已按比值换算到 tick 口径**)"
            )
            + f"→ **V_bucket = {vpin['v_bucket']:,.1f}**(tick 口径)",
            f"- 分布:p50={d.get('p50', float('nan')):.3f} p90={d.get('p90', float('nan')):.3f} "
            f"p95={d.get('p95', float('nan')):.3f} p97={d.get('p97', float('nan')):.3f} "
            f"max={vpin.get('max', float('nan')):.3f}",
            f"- 桶时长(分钟):median={vpin.get('bucket_minutes', {}).get('median', float('nan')):.1f} "
            f"min={vpin.get('bucket_minutes', {}).get('min', float('nan')):.1f} "
            f"max={vpin.get('bucket_minutes', {}).get('max', float('nan')):.1f}",
            f"- lag-1 自相关 = {vpin.get('lag1_autocorr', float('nan')):.3f};"
            f"side-VPIN vs BVC-VPIN 相关 = {vpin.get('side_vs_bvc_corr', float('nan')):.3f}"
            "(side 为主分类,BVC 为无 side 场景的对照验证)",
            f"- 滚动分位 warmup(100 桶):{'已达' if vpin.get('warmed') else '未达'};"
            f"p95={_fmt3(vpin.get('p95'))} p97={_fmt3(vpin.get('p97'))}",
            "",
        ]
    lines += [
        "## 现读(最新 bar)",
        "",
        f"- 结构态 = {struct_states[-1]};最近事件:"
        + (
            f"{struct_events[-1].kind} @ {times[struct_events[-1].idx]:%Y-%m-%d}"
            if struct_events
            else "无"
        ),
        "",
    ]
    return "\n".join(lines)


def _fmt3(x) -> str:
    return f"{x:.3f}" if x is not None else "n/a"


def _build_verdict(
    times,
    n,
    chan1,
    chan2,
    chan3,
    chan3b,
    ict2a,
    ict2b,
    vpin,
    legs,
    close,
    struct_states,
    overlap,
) -> str:
    # BH family: every primary p actually computed
    family: list[tuple[str, float]] = []
    for name, res in chan1.items():
        if "p" in res:
            family.append((f"H-CHAN1[{name}]", res["p"]))
    if "p" in chan2:
        family.append(("H-CHAN2", chan2["p"]))
    family.append(("H-CHAN3a[calm]", chan3["calm"]["p"]))
    family.append(("H-CHAN3a[low_sigma]", chan3["low_sigma"]["p"]))
    if "p_vol" in chan3b:
        family.append(("H-CHAN3b[vol]", chan3b["p_vol"]))
    if ict2b.get("wilcoxon_p") is not None:
        family.append(("H-ICT2b[wilcoxon]", ict2b["wilcoxon_p"]))
    qs = bh_adjust([p for _n, p in family]) if family else []

    lines = _hdr("三视角对比与裁决 v1(lens verdict)", times, n)
    lines += _sample_banner()
    lines += [
        "## 逐假设结果(裸 p 判 pass/fail 按池原文;BH q 同列,FDR 下失守者标注)",
        "",
        "| 假设 | 统计量 | 裸 p | BH q | 判定 |",
        "|---|---|---:|---:|---|",
    ]
    fam_q = {name: q for (name, _p), q in zip(family, qs)}
    for name, res in chan1.items():
        tag = f"H-CHAN1[{name}]"
        if res["verdict"] == "UNDERPOWERED":
            lines.append(
                f"| {tag} | n_A={res['n_a']}, n_C={res['n_c']} | — | — | UNDERPOWERED |"
            )
        else:
            q = fam_q.get(tag, float("nan"))
            note = " ⚠FDR失守" if res["p"] < 0.10 <= q else ""
            lines.append(
                f"| {tag} | Welch t={res['t']:.2f} | {res['p']:.4f} | {q:.4f} | {res['verdict']}{note} |"
            )
    if "p" in chan2:
        q = fam_q.get("H-CHAN2", float("nan"))
        note = " ⚠FDR失守" if chan2["p"] < 0.10 <= q else ""
        lines.append(
            f"| H-CHAN2 | Fisher | {chan2['p']:.4f} | {q:.4f} | {chan2['verdict']}{note} |"
        )
    else:
        lines.append(
            f"| H-CHAN2 | 候选={chan2['n_candidates']} | — | — | {chan2['verdict']} |"
        )
    for key, label in (
        ("calm", "H-CHAN3a[calm]"),
        ("low_sigma", "H-CHAN3a[low_sigma]"),
    ):
        p = chan3[key]["p"]
        lines.append(
            f"| {label} | Fisher lift | {p:.4f} | {fam_q.get(label, float('nan')):.4f} | "
            f"{'pass' if p < 0.10 else 'fail'}(改编;Coiling UNTESTABLE) |"
        )
    if "p_vol" in chan3b:
        lines.append(
            f"| H-CHAN3b | MW(fwd vol) | {chan3b['p_vol']:.4f} | "
            f"{fam_q.get('H-CHAN3b[vol]', float('nan')):.4f} | {chan3b['verdict']} |"
        )
    else:
        lines.append(f"| H-CHAN3b | — | — | — | {chan3b['verdict']} |")
    lines.append(
        f"| H-ICT2a | {ict2a['k']}/{ict2a['n']},CP CI "
        f"[{ict2a['ci'][0] * 100:.0f}%,{ict2a['ci'][1] * 100:.0f}%] | — | — | 点估计(n=13 不作显著性) |"
    )
    w = ict2b.get("wilcoxon_p")
    lines.append(
        f"| H-ICT2b | 配对 n={ict2b['n_pairs']} | {w if w is None else f'{w:.3f}'} | "
        f"{fam_q.get('H-ICT2b[wilcoxon]', float('nan')):.4f} | "
        f"{'descriptive' if ict2b['n_pairs'] < 6 else 'reported'} |"
    )
    lines += [
        "| H-ICT1a/1b | VPIN | — | — | **PENDING(数据不足,≈2026-08-05 补跑)** |",
        "",
        "## 每条 Surging 腿的三视角对照",
        "",
        "| leg | sel(方向/收尾) | 缠论(背驰候选) | ICT(众数结构/lead) |",
        "|---:|---|---|---|",
    ]
    div_by_leg = {lid: nf for lid, nf, _f3, _a in chan2["leg_view"]}
    modal_by_leg = {leg.leg_id: (modal, ok) for leg, modal, ok in ict2a["per_leg"]}
    lead_by_leg = {lid: (lc, ld) for lid, _d, lc, ld in ict2b["rows"]}
    for leg in legs:
        m, ok = modal_by_leg.get(leg.leg_id, ("—", False))
        lc, _ld = lead_by_leg.get(leg.leg_id, (None, None))
        div_cell = (
            f"{div_by_leg[leg.leg_id]} 个候选"
            if leg.leg_id in div_by_leg
            else "不可测(无前腿)"
        )
        lines.append(
            f"| {leg.leg_id} | {'+1' if leg.direction == 1 else leg.direction}/{leg.end_via or '—'} "
            f"| {div_cell} "
            f"| {m}{'✓' if ok else '✗'}"
            + (f",CHoCH lead {lc}" if lc is not None else "")
            + " |"
        )

    # go/no-go per the user-agreed rule: only a CLEAR fail per the pool's 失败标准 stays offline
    def _chan1_clear_fail():
        verdicts = [r["verdict"] for r in chan1.values()]
        return all(v == "fail" for v in verdicts)

    chan3_fail = chan3["verdict_3a"] == "adapted-fail" and chan3b["verdict"] == "fail"
    ict2_fail = (ict2a["k"] / max(1, ict2a["n"])) < 0.50 and not any(
        lc is not None for _l, _d, lc, _ld in ict2b["rows"]
    )
    go = {
        "CHAN-1 (chan_retest)": not _chan1_clear_fail(),
        "CHAN-2 (chan_divergence)": chan2["verdict"] != "fail",
        "CHAN-3 (chan_pivot)": not chan3_fail,
        "ICT-2 (swing_structure)": not ict2_fail,
        "ICT-1 (vpin)": True,  # PENDING → 接入采集,评估期剔除 <30d(池纪律 Month 3)
    }
    lines += [
        "",
        "## Live 接入 go/no-go(规则:仅离线明确触发失败标准者不接;UNDERPOWERED/PENDING 接入继续积累)",
        "",
        "| 候选 | 接入 | 依据 |",
        "|---|---|---|",
    ]
    reasons = {
        "CHAN-1 (chan_retest)": "; ".join(
            f"{k}:{v['verdict']}" for k, v in chan1.items()
        ),
        "CHAN-2 (chan_divergence)": chan2["verdict"],
        "CHAN-3 (chan_pivot)": f"3a:{chan3['verdict_3a']} / 3b:{chan3b['verdict']}",
        "ICT-2 (swing_structure)": f"一致率 {ict2a['k']}/{ict2a['n']}",
        "ICT-1 (vpin)": "PENDING(数据不足)",
    }
    for k, v in go.items():
        lines.append(f"| {k} | {'**GO**' if v else '**NO-GO(废弃)**'} | {reasons[k]} |")
    lines += [
        "",
        "## Live 冻结参数(唯一出处;观察工具引用本文档)",
        "",
        f"- CHAN-1 盘整参数组:主 `K18`(18 bar, 3.0×ATR),敏感性 `K30`(30 bar, 4.0×ATR)——live 用主参数组",
        f"- CHAN-3 overlap 阈值:**p70 = {chan3['p70']:.3f}**(2yr 全样本,in-sample,live 冻结)",
        f"- ICT-2 zigzag:1.5×ATR(14)(与 CHAN-3 共享,substate 同参)",
        f"- ICT-1 VPIN:V_bucket **自适应**(监控服务启动时按 min(30d, 可用) tick 量自举,"
        f"= 日均 tick 量/50;本次研究日取值 {vpin.get('v_bucket', float('nan')):,.1f} tick 口径),"
        f"信号阈值 = 滚动 p95,warmup 100 桶;`history_days` 入 metadata(Month-3 评估剔除 <30d)",
        "",
        f"> {VPIN_RERUN_NOTE}",
        "",
    ]
    return "\n".join(lines)


# ── orchestration ────────────────────────────────────────────────────────────


async def run(skip_ticks: bool = False) -> dict:
    dsn = os.environ["DB_URL"].replace("postgresql+asyncpg://", "postgresql://")
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=3)
    try:
        times, high, low, close, volume, states, via, degraded = await _load_joined(
            pool
        )
        n = len(close)
        logger.info("loaded %d aligned bars for %s", n, SYMBOL)

        atr = compute_atr(high, low, close)
        legs = surging_legs(states, via, close)
        logger.info(
            "surging legs: %d (end_via: %s)",
            len(legs),
            {leg.end_via for leg in legs},
        )

        overlap = pivot_overlap_series(close, atr)
        sigma_pct = sigma_pctile_series(close)
        struct_states, struct_events = structure_series(close, atr)

        chan1 = test_h_chan1(high, low, close, atr)
        chan2 = test_h_chan2(states, via, close)
        chan3 = test_h_chan3(states, overlap, sigma_pct)
        chan3b = test_h_chan3b(states, overlap, close)
        ict2a = test_h_ict2a(states, via, close, struct_states)
        ict2b = test_h_ict2b(states, via, close, struct_events, chan2)

        vpin = {"available": False}
        if not skip_ticks:
            vpin = await _run_vpin_pilot(pool, times, volume)

        reports = {
            "lens_sel_v1.md": _build_sel_report(
                times, close, states, via, degraded, legs
            ),
            "lens_chan_v1.md": _build_chan_report(
                times, close, chan1, chan2, chan3, chan3b, overlap
            ),
            "lens_ict_v1.md": _build_ict_report(
                times, close, struct_states, struct_events, ict2a, ict2b, vpin
            ),
            "lens_verdict_v1.md": _build_verdict(
                times,
                n,
                chan1,
                chan2,
                chan3,
                chan3b,
                ict2a,
                ict2b,
                vpin,
                legs,
                close,
                struct_states,
                overlap,
            ),
        }
        for fname, text in reports.items():
            path = ANALYSIS_DIR / fname
            try:
                path.write_text(text)
                logger.info("report written: %s", path)
            except OSError as exc:
                logger.warning("could not write %s: %s", path, exc)

        print(reports["lens_verdict_v1.md"])
        return {
            "n_bars": n,
            "n_legs": len(legs),
            "chan1": {k: v["verdict"] for k, v in chan1.items()},
            "chan2": chan2["verdict"],
            "chan3a": chan3["verdict_3a"],
            "chan3b": chan3b["verdict"],
            "ict2a": f"{ict2a['k']}/{ict2a['n']}",
            "vpin_available": vpin.get("available", False),
        }
    finally:
        await pool.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-ticks", action="store_true", help="bar lenses only")
    args = parser.parse_args()
    asyncio.run(run(skip_ticks=args.skip_ticks))
