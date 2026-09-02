#!/usr/bin/env python3
"""
monitor/daily_report.py

CryptoWatch v4 每日健康简报
用法：
  python daily_report.py              # 输出到终端 + 保存 JSON
  python daily_report.py --push       # 同时推送 Telegram
  python daily_report.py --days 7     # 统计过去7天（默认1天）

输出内容：
  1. IC / IR 统计（alpha 有效性）
  2. 信号质量分布（频率 / 胜率预测 / 类型分布）
  3. Regime 分布（市场状态）
  4. 风控状态（drawdown / circuit breaker）
  5. 执行统计（成交率 / 滑点 / PnL）
  6. 切换建议（NOTIFY_ONLY → CONFIRM → AUTO）
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta

import redis as _redis_sync

# ── 配置 ──────────────────────────────────────────────
REDIS_URL = os.environ.get("REDIS_URL", "redis://:changeme@redis:6379/0")
GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://localhost:5000")
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")

# 切换门槛（用于自动建议）
THRESHOLDS = {
    "ic_min_for_confirm": 0.05,  # IC 均值达到此值才建议切 CONFIRM
    "ic_min_for_auto": 0.08,  # IC 均值达到此值才建议切 AUTO
    "ir_min_for_confirm": 0.5,
    "ir_min_for_auto": 1.0,
    "signal_freq_max": 8,  # 每天信号数超过此值视为过多
    "signal_freq_min": 1,  # 每天信号数低于此值视为过少
    "win_prob_avg_min": 0.57,  # 平均胜率须高于此值
    "regime_stability_min": 0.6,  # 最主要 regime 占比须高于此值（稳定）
    "drawdown_max_confirm": 0.08,  # 当前回撤低于此值才建议切 CONFIRM
    "drawdown_max_auto": 0.05,
}

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "AVAXUSDT"]


# ── Redis 连接 ────────────────────────────────────────
def get_redis() -> _redis_sync.Redis:
    return _redis_sync.from_url(REDIS_URL, decode_responses=True)


# ── 数据采集 ──────────────────────────────────────────


def fetch_ic_stats(r: _redis_sync.Redis, symbols: list) -> dict:
    """从 cw4:ic_stats 读取各 symbol 的 IC/IR 统计"""
    result = {}
    for sym in symbols:
        raw = r.hget("cw4:ic_stats", sym)
        if raw:
            try:
                result[sym] = json.loads(raw)
            except Exception:
                pass
    return result


def fetch_recent_signals(r: _redis_sync.Redis, days: int) -> list:
    """从 cw4:signals:recent hash 读取信号，过滤最近 N 天"""
    cutoff = time.time() - days * 86400
    raw = r.hgetall("cw4:signals:recent") or {}
    signals = []
    for v in raw.values():
        try:
            s = json.loads(v)
            ts_str = s.get("timestamp", "")
            if ts_str:
                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    if ts.timestamp() >= cutoff:
                        signals.append(s)
                except Exception:
                    signals.append(s)  # 无法解析时间则包含
        except Exception:
            pass
    return signals


def fetch_regime_distribution(r: _redis_sync.Redis) -> dict:
    """从 cw4:regimes 读取各 symbol 当前 regime"""
    raw = r.hgetall("cw4:regimes") or {}
    distribution = {}
    details = {}
    for sym, v in raw.items():
        try:
            d = json.loads(v)
            regime = d.get("regime", "UNKNOWN")
            distribution[regime] = distribution.get(regime, 0) + 1
            details[sym] = {
                "regime": regime,
                "bars_in_regime": d.get("bars_in_regime", 0),
                "data_points": d.get("data_points", 0),
            }
        except Exception:
            pass
    return {"distribution": distribution, "details": details, "total": len(raw)}


def fetch_risk_status(r: _redis_sync.Redis) -> dict:
    """从 cw4:risk:status 读取风控状态"""
    raw = r.get("cw4:risk:status")
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    return {}


def fetch_portfolio_state(r: _redis_sync.Redis) -> dict:
    """从 cw4:portfolio:state 读取持仓状态"""
    raw = r.get("cw4:portfolio:state")
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    return {}


def fetch_recent_orders(r: _redis_sync.Redis, days: int) -> list:
    """从 cw4:orders:recent 读取最近成交"""
    cutoff = time.time() - days * 86400
    raw = r.hgetall("cw4:orders:recent") or {}
    orders = []
    for v in raw.values():
        try:
            o = json.loads(v)
            ts_str = o.get("created_at", "")
            if ts_str:
                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    if ts.timestamp() >= cutoff:
                        orders.append(o)
                except Exception:
                    orders.append(o)
        except Exception:
            pass
    return orders


def fetch_onchain_state(r: _redis_sync.Redis) -> dict:
    """读取 onchain-sentinel 状态"""
    result = {}
    for sym in ["BTCUSDT", "ETHUSDT", "SOLUSDT"]:
        raw = r.get(f"onchain:state:{sym}")
        if raw:
            try:
                result[sym] = json.loads(raw)
            except Exception:
                pass
    return result


# ── 分析计算 ──────────────────────────────────────────


def analyze_ic(ic_stats: dict) -> dict:
    """汇总所有 symbol 的 IC/IR，计算加权均值"""
    if not ic_stats:
        return {"status": "no_data", "mean_ic": None, "mean_ir": None, "total_n": 0}

    ics = [v["ic"] for v in ic_stats.values() if v.get("ic") is not None]
    irs = [v["ir"] for v in ic_stats.values() if v.get("ir") is not None]
    ns = [v["n"] for v in ic_stats.values() if v.get("n", 0) > 0]

    mean_ic = sum(ics) / len(ics) if ics else None
    mean_ir = sum(irs) / len(irs) if irs else None
    total_n = sum(ns)

    return {
        "status": "ok",
        "mean_ic": round(mean_ic, 4) if mean_ic is not None else None,
        "mean_ir": round(mean_ir, 4) if mean_ir is not None else None,
        "total_n": total_n,
        "by_symbol": {
            sym: {
                "ic": v.get("ic"),
                "ir": v.get("ir"),
                "n": v.get("n", 0),
                "mean_ret_pct": round(v.get("mean_return", 0) * 100, 3),
            }
            for sym, v in ic_stats.items()
        },
    }


def analyze_signals(signals: list, days: int) -> dict:
    """分析信号质量分布"""
    if not signals:
        return {"status": "no_data", "count": 0}

    total = len(signals)
    per_day = total / max(days, 1)
    win_probs = [float(s.get("win_probability", 0)) for s in signals]
    avg_prob = sum(win_probs) / len(win_probs) if win_probs else 0
    high_conf = sum(1 for p in win_probs if p >= 0.62)

    # 类型分布
    type_dist: dict = {}
    for s in signals:
        t = s.get("signal_type", "UNKNOWN")
        type_dist[t] = type_dist.get(t, 0) + 1

    # 方向分布
    dir_dist: dict = {}
    for s in signals:
        d = s.get("direction", "UNKNOWN")
        dir_dist[d] = dir_dist.get(d, 0) + 1

    # symbol 分布
    sym_dist: dict = {}
    for s in signals:
        sym = s.get("symbol", "UNKNOWN")
        sym_dist[sym] = sym_dist.get(sym, 0) + 1

    # regime 分布（信号触发时的 regime）
    sig_regime_dist: dict = {}
    for s in signals:
        rg = s.get("regime", "UNKNOWN")
        sig_regime_dist[rg] = sig_regime_dist.get(rg, 0) + 1

    return {
        "status": "ok",
        "total": total,
        "per_day": round(per_day, 1),
        "avg_win_prob": round(avg_prob, 4),
        "high_conf_count": high_conf,
        "high_conf_pct": round(high_conf / total * 100, 1) if total else 0,
        "type_dist": type_dist,
        "dir_dist": dir_dist,
        "symbol_dist": dict(sorted(sym_dist.items(), key=lambda x: -x[1])[:8]),
        "regime_dist": sig_regime_dist,
    }


def analyze_regime(regime_data: dict) -> dict:
    """分析 regime 分布稳定性"""
    dist = regime_data.get("distribution", {})
    total = regime_data.get("total", 0)
    if not dist or not total:
        return {"status": "no_data"}

    dominant = max(dist, key=dist.get)
    dominant_pct = dist[dominant] / total
    high_vol_count = dist.get("HIGH_VOLATILITY", 0)
    unknown_count = dist.get("UNKNOWN", 0)

    return {
        "status": "ok",
        "dominant": dominant,
        "dominant_pct": round(dominant_pct, 3),
        "distribution": {k: round(v / total, 3) for k, v in dist.items()},
        "high_vol_pct": round(high_vol_count / total, 3) if total else 0,
        "unknown_pct": round(unknown_count / total, 3) if total else 0,
        "is_stable": dominant_pct >= THRESHOLDS["regime_stability_min"],
        "symbol_count": total,
    }


def analyze_orders(orders: list, days: int) -> dict:
    """分析成交质量"""
    if not orders:
        return {"status": "no_data", "count": 0}

    filled = [o for o in orders if o.get("state") in ("FILLED", "MONITORING", "CLOSED")]
    failed = [o for o in orders if o.get("state") in ("FAILED",)]
    cancelled = [o for o in orders if o.get("state") == "CANCELLED"]
    closed = [o for o in orders if o.get("state") == "CLOSED"]

    fill_rate = len(filled) / len(orders) if orders else 0

    slippages = [float(o.get("slippage_pct", 0)) for o in filled if o.get("slippage_pct")]
    avg_slip = sum(slippages) / len(slippages) if slippages else 0

    pnls = [float(o.get("realized_pnl", 0)) for o in closed if o.get("realized_pnl") is not None]
    total_pnl = sum(pnls)
    win_count = sum(1 for p in pnls if p > 0)
    win_rate = win_count / len(pnls) if pnls else None

    return {
        "status": "ok",
        "total": len(orders),
        "filled": len(filled),
        "failed": len(failed),
        "cancelled": len(cancelled),
        "closed": len(closed),
        "fill_rate": round(fill_rate, 3),
        "avg_slip_pct": round(avg_slip, 4),
        "total_pnl": round(total_pnl, 4),
        "win_rate": round(win_rate, 3) if win_rate is not None else None,
        "per_day": round(len(orders) / max(days, 1), 1),
    }


# ── 切换建议引擎 ──────────────────────────────────────


def generate_recommendation(
    ic_analysis: dict,
    signal_analysis: dict,
    regime_analysis: dict,
    risk_status: dict,
    order_analysis: dict,
    current_mode: str,
) -> dict:
    """
    输出执行模式切换门槛的【观察状态】（哪些门槛已满足/被阻断）。
    遵守 Helios "只观察不建议" 铁律 + 实盘安全纪律:本函数不作切换建议,
    是否切换模式始终是人工决策。`next_mode` 仅表示"门槛已满足的模式",非推荐。
    返回 {next_mode, confidence, action(观察陈述), reasons, blockers}
    """
    T = THRESHOLDS
    reasons = []
    blockers = []

    mean_ic = ic_analysis.get("mean_ic")
    mean_ir = ic_analysis.get("mean_ir")
    total_n = ic_analysis.get("total_n", 0)
    per_day = signal_analysis.get("per_day", 0)
    avg_prob = signal_analysis.get("avg_win_prob", 0)
    is_stable = regime_analysis.get("is_stable", False)
    dd_pct = risk_status.get("current_dd_pct", 0) / 100 if risk_status else 0
    circuit = risk_status.get("circuit_broken", False)
    fill_rate = order_analysis.get("fill_rate")

    # ── 硬性阻断条件 ──────────────────────────────────
    if circuit:
        blockers.append("🔴 熔断器已触发，必须手动 reset 后才能切换")
    if dd_pct > T["drawdown_max_confirm"]:
        blockers.append(f"🔴 当前回撤 {dd_pct:.1%} > 阈值 {T['drawdown_max_confirm']:.0%}")
    if total_n < 20:
        blockers.append(f"🟡 IC 样本量不足（{total_n} 条，需 ≥ 20）")
    if mean_ic is None:
        blockers.append("🟡 IC 数据尚未积累，继续观察")
    if per_day > T["signal_freq_max"]:
        blockers.append(f"🟡 信号过于频繁（{per_day:.1f} 次/天），需排查过拟合")
    if per_day < T["signal_freq_min"] and per_day > 0:
        blockers.append(f"🟡 信号过少（{per_day:.1f} 次/天），策略可能过于保守")

    # ── 正向指标 ──────────────────────────────────────
    if mean_ic and mean_ic >= T["ic_min_for_confirm"]:
        reasons.append(f"✅ IC 均值 {mean_ic:.4f} ≥ 阈值 {T['ic_min_for_confirm']}")
    if mean_ir and mean_ir >= T["ir_min_for_confirm"]:
        reasons.append(f"✅ IR {mean_ir:.2f} ≥ 阈值 {T['ir_min_for_confirm']}")
    if avg_prob >= T["win_prob_avg_min"]:
        reasons.append(f"✅ 平均胜率预测 {avg_prob:.0%} ≥ {T['win_prob_avg_min']:.0%}")
    if is_stable:
        reasons.append(f"✅ Regime 稳定（主导 regime 占 {regime_analysis.get('dominant_pct', 0):.0%}）")
    if fill_rate and fill_rate >= 0.85:
        reasons.append(f"✅ 成交率 {fill_rate:.0%}")

    # ── 决策逻辑 ──────────────────────────────────────
    if blockers:
        return {
            "next_mode": current_mode,
            "confidence": "LOW",
            "action": "切换门槛被阻断，维持当前模式（观察）",
            "reasons": reasons,
            "blockers": blockers,
        }

    # 判断是否可以从 NOTIFY → CONFIRM
    can_confirm = (
        mean_ic is not None
        and mean_ic >= T["ic_min_for_confirm"]
        and (mean_ir is None or mean_ir >= T["ir_min_for_confirm"])
        and avg_prob >= T["win_prob_avg_min"]
        and T["signal_freq_min"] <= per_day <= T["signal_freq_max"]
        and dd_pct <= T["drawdown_max_confirm"]
    )

    # 判断是否可以从 CONFIRM → AUTO
    can_auto = (
        can_confirm
        and mean_ic >= T["ic_min_for_auto"]
        and (mean_ir is None or mean_ir >= T["ir_min_for_auto"])
        and dd_pct <= T["drawdown_max_auto"]
        and fill_rate is not None
        and fill_rate >= 0.90
        and total_n >= 50
    )

    if current_mode == "NOTIFY_ONLY":
        if can_confirm:
            return {
                "next_mode": "CONFIRM_THEN_EXEC",
                "confidence": "HIGH" if len(reasons) >= 4 else "MEDIUM",
                "action": "CONFIRM_THEN_EXEC 的切换门槛已满足（是否切换为人工决策，系统不作建议）",
                "reasons": reasons,
                "blockers": [],
            }

    elif current_mode == "CONFIRM_THEN_EXEC":
        if can_auto:
            return {
                "next_mode": "AUTO_EXEC",
                "confidence": "HIGH" if len(reasons) >= 5 else "MEDIUM",
                "action": "AUTO_EXEC 的切换门槛已满足（是否切换为人工决策，系统不作建议）",
                "reasons": reasons,
                "blockers": [],
            }

    return {
        "next_mode": current_mode,
        "confidence": "MEDIUM",
        "action": "门槛尚未满足，继续积累数据（观察）",
        "reasons": reasons,
        "blockers": [],
    }


# ── 报告渲染 ──────────────────────────────────────────


def render_report(data: dict, days: int) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    ic = data["ic"]
    signals = data["signals"]
    regimes = data["regimes"]
    risk = data["risk"]
    portfolio = data["portfolio"]
    orders = data["orders"]
    onchain = data["onchain"]
    rec = data["recommendation"]
    mode = data["current_mode"]

    lines = []

    def h(title: str):
        lines.append(f"\n{'━' * 48}")
        lines.append(f"  {title}")
        lines.append(f"{'━' * 48}")

    def row(label: str, value, warn: str = ""):
        w = f"  ⚠ {warn}" if warn else ""
        lines.append(f"  {label:<28} {value}{w}")

    # ── 头部 ──
    lines.append(f"""
╔{"═" * 50}╗
║  CryptoWatch v4 · 每日健康简报{" " * 19}║
║  {now}  ·  统计周期: 过去 {days} 天{" " * (20 - len(str(days)))}║
╚{"═" * 50}╝""")

    # ── 当前模式 ──
    mode_icon = {"NOTIFY_ONLY": "👁", "CONFIRM_THEN_EXEC": "🤝", "AUTO_EXEC": "🤖"}.get(mode, "?")
    h(f"当前运行模式  {mode_icon}  {mode}")

    # ── IC / IR（Alpha 有效性）──
    h("① IC / IR 统计  —  Alpha 有效性")
    if ic["status"] == "no_data":
        lines.append("  ⏳ 尚无 IC 数据，继续运行积累中...")
        lines.append(f"  样本需求: ≥ 20 条  当前: {ic.get('total_n', 0)} 条")
    else:
        mean_ic = ic.get("mean_ic")
        mean_ir = ic.get("mean_ir")

        ic_status = (
            "🔴 信号接近随机"
            if mean_ic is not None and mean_ic < 0.03
            else "🟡 信号弱，需观察"
            if mean_ic is not None and mean_ic < 0.05
            else "🟢 信号有效"
            if mean_ic is not None
            else "⏳ 计算中"
        )
        row("IC 均值", f"{mean_ic:.4f}" if mean_ic else "N/A", "" if (mean_ic or 0) >= 0.05 else "低于切换阈值 0.05")
        row("IR（稳定性）", f"{mean_ir:.2f}" if mean_ir else "N/A")
        row("IC 样本量", ic["total_n"])
        row("Alpha 判断", ic_status)
        lines.append("")
        lines.append("  各 Symbol 明细:")
        for sym, v in ic.get("by_symbol", {}).items():
            ic_v = v.get("ic")
            ir_v = v.get("ir")
            n_v = v.get("n", 0)
            ret_v = v.get("mean_ret_pct", 0)
            lines.append(
                f"    {sym:<12} IC={ic_v:.4f}  IR={ir_v:.2f}  n={n_v}  均收益={ret_v:+.3f}%"
                if ic_v is not None and ir_v is not None
                else f"    {sym:<12} 数据不足 (n={n_v})"
            )

    # ── 信号质量 ──
    h("② 信号质量分布")
    if signals["status"] == "no_data":
        lines.append("  ⏳ 尚无信号数据")
    else:
        freq_warn = (
            "⚠ 过多，检查过拟合"
            if signals["per_day"] > THRESHOLDS["signal_freq_max"]
            else "⚠ 过少，策略过严"
            if signals["per_day"] < THRESHOLDS["signal_freq_min"]
            else ""
        )
        prob_warn = "" if signals["avg_win_prob"] >= THRESHOLDS["win_prob_avg_min"] else "低于阈值 0.57"
        row("信号总数", signals["total"])
        row("信号频率", f"{signals['per_day']:.1f} 次/天", freq_warn)
        row("平均胜率预测", f"{signals['avg_win_prob']:.2%}", prob_warn)
        row("高置信度信号", f"{signals['high_conf_count']} 条 ({signals['high_conf_pct']:.1f}%)")
        lines.append("")
        lines.append("  类型分布:")
        for t, cnt in sorted(signals.get("type_dist", {}).items(), key=lambda x: -x[1]):
            bar = "█" * min(cnt, 20)
            lines.append(f"    {t:<22} {cnt:>4}  {bar}")
        lines.append("")
        lines.append("  方向分布:")
        for d, cnt in signals.get("dir_dist", {}).items():
            pct = cnt / signals["total"] * 100
            lines.append(f"    {d:<10} {cnt:>4} ({pct:.1f}%)")

    # ── Regime 分布 ──
    h("③ Regime 分布  —  市场状态")
    if regimes["status"] == "no_data":
        lines.append("  ⏳ 尚无 Regime 数据")
    else:
        stability = "🟢 稳定" if regimes["is_stable"] else "🔴 不稳定（频繁切换）"
        row("主导 Regime", f"{regimes['dominant']} ({regimes['dominant_pct']:.0%})")
        row("稳定性判断", stability)
        row(
            "高波动占比",
            f"{regimes['high_vol_pct']:.0%}",
            "当前处于高波动，策略受限" if regimes["high_vol_pct"] > 0.3 else "",
        )
        row("UNKNOWN 占比", f"{regimes['unknown_pct']:.0%}", "数据量不足" if regimes["unknown_pct"] > 0.2 else "")
        lines.append("")
        lines.append("  Regime 分布（占监控 symbol 比例）:")
        for rg, pct in sorted(regimes["distribution"].items(), key=lambda x: -x[1]):
            bar_len = int(pct * 30)
            bar = "█" * bar_len + "░" * (30 - bar_len)
            icon = {
                "TRENDING_UP": "📈",
                "TRENDING_DOWN": "📉",
                "RANGING": "↔️",
                "HIGH_VOLATILITY": "⚡",
                "ACCUMULATION": "🔄",
                "UNKNOWN": "❓",
            }.get(rg, "  ")
            lines.append(f"    {icon} {rg:<18} {pct:.0%}  {bar}")

    # ── 风控状态 ──
    h("④ 风控状态")
    if not risk:
        lines.append("  ⏳ Risk service 未连接")
    else:
        dd = risk.get("current_dd_pct", 0)
        level = risk.get("level", "UNKNOWN")
        new_t = risk.get("new_trades_allowed", True)
        circ = risk.get("circuit_broken", False)
        daily = risk.get("daily_pnl", 0)

        level_icon = {"GREEN": "🟢", "YELLOW": "🟡", "ORANGE": "🟠", "RED": "🔴"}.get(level, "⚪")
        row("Drawdown 级别", f"{level_icon} {level}")
        row("当前回撤", f"{dd:.2f}%", "超过切换阈值" if dd / 100 > THRESHOLDS["drawdown_max_confirm"] else "")
        row("新仓允许", "✅ 是" if new_t else "❌ 否（已限制）")
        row("熔断器", "🔴 已触发" if circ else "🟢 正常")
        row("今日 PnL", f"${daily:+.2f}" if daily else "N/A")

    # ── 持仓状态 ──
    h("⑤ 持仓 / 资金状态")
    if not portfolio:
        lines.append("  ⏳ Portfolio service 未连接")
    else:
        eq = portfolio.get("total_equity", 0)
        exp = portfolio.get("total_exposure", 0)
        lev = portfolio.get("leverage", 0)
        cnt = portfolio.get("position_count", 0)
        pnl = portfolio.get("total_pnl", 0)
        row("总净值", f"${eq:,.2f}")
        row("持仓数量", cnt)
        row("总敞口", f"${exp:,.2f}")
        row("杠杆", f"{lev:.2f}x")
        row("累计 PnL", f"${pnl:+,.2f}")

    # ── 执行统计 ──
    h("⑥ 执行统计")
    if orders["status"] == "no_data":
        lines.append("  ⏳ 暂无成交记录（NOTIFY_ONLY 模式正常）")
    else:
        row("总订单数", orders["total"])
        row("成交率", f"{orders['fill_rate']:.0%}")
        row("平均滑点", f"{orders['avg_slip_pct']:.4f}%")
        row("已平仓数", orders["closed"])
        if orders["win_rate"] is not None:
            row("实际胜率", f"{orders['win_rate']:.0%}")
        row("实现 PnL", f"${orders['total_pnl']:+.4f}")

    # ── 链上状态 ──
    if onchain:
        h("⑦ 链上 Sentinel 状态")
        for sym, state in onchain.items():
            score = state.get("onchain_score", 0)
            regime = state.get("regime", "?")
            fused = state.get("fused_win_prob")
            score_bar = "▲" if score > 0.1 else ("▼" if score < -0.1 else "─")
            fused_str = f"融合胜率={fused:.0%}" if fused else ""
            lines.append(f"  {sym:<12} score={score:+.3f} {score_bar}  regime={regime}  {fused_str}")

    # ── 切换门槛状态（观察，不作建议）──
    h("⑧ 模式切换门槛状态")
    conf_icon = {"HIGH": "🟢", "MEDIUM": "🟡", "LOW": "🔴"}.get(rec["confidence"], "⚪")
    lines.append(f"  {conf_icon} 门槛达成度: {rec['confidence']}")
    lines.append(f"  👁 观察: {rec['action']}")
    if rec.get("next_mode") != mode:
        lines.append(f"  🔄 当前: {mode} ｜ 门槛已满足的模式: {rec['next_mode']}（是否切换为人工决策）")
    if rec["reasons"]:
        lines.append("\n  达标指标:")
        for r in rec["reasons"]:
            lines.append(f"    {r}")
    if rec["blockers"]:
        lines.append("\n  阻断因素（必须解决）:")
        for b in rec["blockers"]:
            lines.append(f"    {b}")

    # ── 切换操作说明（仅供人工决定切换时参考，系统不作建议）──
    if rec.get("next_mode") != mode and not rec["blockers"]:
        next_m = rec["next_mode"]
        lines.append(f"""
  ⚠️ 系统只观察、不建议切换；若人工决定切换，操作步骤（修改 .env 后重启服务）:
    EXEC_MODE={next_m}
    {"KELLY_FRACTION=0.25  # 保守值" if next_m == "AUTO_EXEC" else ""}
    docker compose restart execution-service""")

    # ── 底部 ──
    lines.append(f"\n{'━' * 48}")
    lines.append(f"  生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"  下次观察检查: {(datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d 09:00')}")
    lines.append(f"{'━' * 48}\n")

    return "\n".join(lines)


# ── Telegram 推送 ─────────────────────────────────────


def push_telegram(text: str) -> bool:
    if not TG_TOKEN or not TG_CHAT:
        print("  Telegram 未配置，跳过推送")
        return False
    try:
        import urllib.parse
        import urllib.request

        # Telegram 消息长度限制 4096，超出分段发送
        chunks = [text[i : i + 4000] for i in range(0, len(text), 4000)]
        for chunk in chunks:
            payload = json.dumps(
                {
                    "chat_id": TG_CHAT,
                    "text": f"```\n{chunk}\n```",
                    "parse_mode": "Markdown",
                }
            ).encode()
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=10)
        print("  ✅ Telegram 推送成功")
        return True
    except Exception as e:
        print(f"  ❌ Telegram 推送失败: {e}")
        return False


# ── 主流程 ────────────────────────────────────────────


def run(days: int = 1, push: bool = False, current_mode: str = "NOTIFY_ONLY") -> dict:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 开始采集数据...")

    try:
        r = get_redis()
        r.ping()
        print("  ✅ Redis 连接正常")
    except Exception as e:
        print(f"  ❌ Redis 连接失败: {e}")
        print("     检查 REDIS_URL 环境变量是否正确")
        sys.exit(1)

    # 采集
    print("  采集 IC 统计...")
    ic_raw = fetch_ic_stats(r, SYMBOLS)

    print("  采集信号数据...")
    signals_raw = fetch_recent_signals(r, days)

    print("  采集 Regime 数据...")
    regime_raw = fetch_regime_distribution(r)

    print("  采集风控状态...")
    risk_raw = fetch_risk_status(r)

    print("  采集持仓状态...")
    portfolio_raw = fetch_portfolio_state(r)

    print("  采集成交记录...")
    orders_raw = fetch_recent_orders(r, days)

    print("  采集链上状态...")
    onchain_raw = fetch_onchain_state(r)

    # 分析
    print("  分析中...")
    ic_analysis = analyze_ic(ic_raw)
    signal_analysis = analyze_signals(signals_raw, days)
    regime_analysis = analyze_regime(regime_raw)
    order_analysis = analyze_orders(orders_raw, days)

    rec = generate_recommendation(
        ic_analysis,
        signal_analysis,
        regime_analysis,
        risk_raw,
        order_analysis,
        current_mode,
    )

    data = {
        "generated_at": datetime.now().isoformat(),
        "days": days,
        "current_mode": current_mode,
        "ic": ic_analysis,
        "signals": signal_analysis,
        "regimes": regime_analysis,
        "risk": risk_raw,
        "portfolio": portfolio_raw,
        "orders": order_analysis,
        "onchain": onchain_raw,
        "recommendation": rec,
    }

    # 渲染
    report = render_report(data, days)
    print("\n" + report)

    # 保存 JSON
    out_dir = os.path.join(os.path.dirname(__file__), "reports")
    os.makedirs(out_dir, exist_ok=True)
    fname = os.path.join(out_dir, f"report_{datetime.now().strftime('%Y%m%d_%H%M')}.json")
    with open(fname, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"  📁 JSON 已保存: {fname}")

    # 保存文本
    txt_fname = fname.replace(".json", ".txt")
    with open(txt_fname, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"  📁 文本已保存: {txt_fname}")

    # Telegram 推送
    if push:
        print("  推送 Telegram...")
        push_telegram(report)

    return data


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CryptoWatch v4 每日健康简报")
    parser.add_argument("--days", type=int, default=1, help="统计过去 N 天（默认 1）")
    parser.add_argument("--push", action="store_true", help="推送到 Telegram")
    parser.add_argument(
        "--mode",
        default=os.environ.get("EXEC_MODE", "NOTIFY_ONLY"),
        choices=["NOTIFY_ONLY", "CONFIRM_THEN_EXEC", "AUTO_EXEC"],
        help="当前运行模式（默认读 EXEC_MODE 环境变量）",
    )
    args = parser.parse_args()

    run(days=args.days, push=args.push, current_mode=args.mode)
