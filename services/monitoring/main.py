"""
services/monitoring/main.py  —  CryptoWatch v4 Monitoring Service（第24个服务）

功能：
  - 每天 CST 09:00 自动生成健康简报并推送 Telegram
  - 追踪 IC/IR 历史趋势，连续达标时发送切换通知
  - 暴露 /health + /report + /trend REST API 供 gateway 聚合
  - 把简报写入 TimescaleDB audit_log（可回溯）
"""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import PlainTextResponse

from shared.db.connections import get_redis, redis_health

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# ── 配置 ──────────────────────────────────────────────
REPORT_HOUR  = int(os.getenv("REPORT_HOUR_CST", "9"))    # 每天几点生成（CST）
REPORT_DAYS  = int(os.getenv("REPORT_DAYS",     "1"))    # 默认统计过去1天
PUSH_ENABLED = os.getenv("REPORT_PUSH", "true").lower() == "true"
EXEC_MODE    = os.getenv("EXEC_MODE", "NOTIFY_ONLY")
CONSECUTIVE_REQUIRED = int(os.getenv("CONSECUTIVE_DAYS_REQUIRED", "3"))

# 趋势数据存 Redis（不依赖本地文件，容器重启不丢失）
TREND_KEY    = "cw4:monitor:trend"
LAST_RUN_KEY = "cw4:monitor:last_run_date"


# ── 趋势存取（Redis）─────────────────────────────────

async def load_trend() -> list:
    r = await get_redis()
    raw = await r.get(TREND_KEY)
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    return []


async def save_trend(trend: list):
    r = await get_redis()
    await r.set(TREND_KEY, json.dumps(trend, ensure_ascii=False))


async def append_trend(report_data: dict, trend: list) -> list:
    ic   = report_data.get("ic", {})
    sigs = report_data.get("signals", {})
    rec  = report_data.get("recommendation", {})
    risk = report_data.get("risk", {})

    entry = {
        "date":           datetime.now().strftime("%Y-%m-%d"),
        "mean_ic":        ic.get("mean_ic"),
        "mean_ir":        ic.get("mean_ir"),
        "ic_n":           ic.get("total_n", 0),
        "signal_per_day": sigs.get("per_day", 0),
        "avg_win_prob":   sigs.get("avg_win_prob", 0),
        "drawdown_pct":   risk.get("current_dd_pct", 0),
        "next_mode":      rec.get("next_mode"),
        "confidence":     rec.get("confidence"),
        "blockers":       len(rec.get("blockers", [])),
    }

    trend.append(entry)
    return trend[-90:]   # 只保留最近90天


def analyze_trend(trend: list) -> dict:
    if len(trend) < 5:
        return {
            "improving": None,
            "consecutive_pass": 0,
            "msg": f"数据不足（{len(trend)}/5天）",
            "avg_recent": None,
        }

    recent  = [t["mean_ic"] for t in trend[-7:]  if t["mean_ic"] is not None]
    earlier = [t["mean_ic"] for t in trend[-14:-7] if t["mean_ic"] is not None]

    avg_recent  = sum(recent)  / len(recent)  if recent  else None
    avg_earlier = sum(earlier) / len(earlier) if earlier else avg_recent

    if avg_recent is None:
        return {"improving": None, "consecutive_pass": 0,
                "msg": "近期无 IC 数据", "avg_recent": None}

    improving = avg_recent > (avg_earlier or 0) + 0.005
    declining = avg_recent < (avg_earlier or 0) - 0.005

    msg = (
        f"📈 IC 趋势改善 ({avg_earlier:.4f} → {avg_recent:.4f})" if improving else
        f"📉 IC 趋势下降 ({avg_earlier:.4f} → {avg_recent:.4f})" if declining else
        f"➡️  IC 趋势持平 ({avg_recent:.4f})"
    )

    # 连续达标天数
    consecutive = 0
    for t in reversed(trend):
        if (t["mean_ic"] or 0) >= 0.05 and t["blockers"] == 0:
            consecutive += 1
        else:
            break

    return {
        "improving":        improving,
        "consecutive_pass": consecutive,
        "avg_recent":       round(avg_recent, 4),
        "avg_earlier":      round(avg_earlier, 4) if avg_earlier else None,
        "msg":              msg,
    }


# ── 报告生成核心 ──────────────────────────────────────

def _run_report_sync(days: int) -> dict:
    """
    同步执行报告生成（report.py 使用同步 Redis 客户端）。
    在 asyncio.to_thread 中调用，不阻塞事件循环。
    """
    import services.monitoring.report as rpt
    return rpt.run(days=days, push=False, current_mode=EXEC_MODE)


async def generate_and_push(days: int = REPORT_DAYS) -> dict:
    """生成报告、更新趋势、推送通知的完整流程"""
    logger.info(f"开始生成每日简报 (days={days})...")

    # 在线程池里跑同步报告逻辑
    report_data = await asyncio.to_thread(_run_report_sync, days)

    # 更新趋势
    trend = await load_trend()
    trend = await append_trend(report_data, trend)
    await save_trend(trend)

    # 趋势分析
    trend_analysis = analyze_trend(trend)
    logger.info(f"趋势分析: {trend_analysis['msg']}")

    # 生成最终推送文本
    import services.monitoring.report as rpt
    report_text = rpt.render_report(report_data, days)

    # 追加趋势信息到推送文本
    trend_append = _build_trend_section(trend, trend_analysis)
    full_text    = report_text + trend_append

    # 推送 Telegram
    if PUSH_ENABLED:
        _push_telegram(full_text)

    # 连续达标特别通知
    if trend_analysis["consecutive_pass"] >= CONSECUTIVE_REQUIRED:
        last = trend[-1] if trend else {}
        next_mode = last.get("next_mode", "")
        if next_mode and next_mode != EXEC_MODE:
            _push_telegram(
                f"🎉 CryptoWatch v4 连续 {trend_analysis['consecutive_pass']} 天满足切换条件！\n\n"
                f"建议切换: {EXEC_MODE} → {next_mode}\n"
                f"IC 均值（近7天）: {trend_analysis['avg_recent']:.4f}\n\n"
                f"操作：修改 .env EXEC_MODE={next_mode}\n"
                f"然后运行：docker compose restart execution-service"
            )

    # 写 Redis 缓存（供 gateway API 读取）
    r = await get_redis()
    await r.set("cw4:monitor:latest_report", json.dumps({
        "data":           report_data,
        "trend":          trend_analysis,
        "generated_at":   datetime.now().isoformat(),
        "days":           days,
    }), ex=86400 * 2)   # 缓存2天

    # 记录最后运行日期
    await r.set(LAST_RUN_KEY, datetime.now().strftime("%Y-%m-%d"))

    logger.info("✅ 每日简报完成")
    return {"report": report_data, "trend": trend_analysis}


def _build_trend_section(trend: list, analysis: dict) -> str:
    """构建趋势追加段落"""
    lines = [
        "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "  ⑨ IC 历史趋势（过去14天）",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
    ]

    ics = [(t["date"][-5:], t["mean_ic"]) for t in trend[-14:]
           if t["mean_ic"] is not None]

    if ics:
        max_ic = max(v for _, v in ics) or 0.01
        for date, ic in ics:
            bar_len = int(ic / max_ic * 25) if max_ic > 0 else 0
            bar     = "█" * bar_len
            tag     = " ◀ 切换阈值" if 0.048 < ic < 0.052 else ""
            lines.append(f"  {date}  {ic:.4f}  {bar}{tag}")
    else:
        lines.append("  （暂无历史数据）")

    lines += [
        "",
        f"  {analysis['msg']}",
        f"  连续达标天数: {analysis['consecutive_pass']} / 需要 {CONSECUTIVE_REQUIRED} 天",
        "",
    ]
    return "\n".join(lines)


def _push_telegram(text: str):
    if not (os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID")):
        logger.info("Telegram 未配置，跳过推送")
        return
    token   = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    try:
        import urllib.request
        chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
        for chunk in chunks:
            payload = json.dumps({
                "chat_id":    chat_id,
                "text":       f"```\n{chunk}\n```",
                "parse_mode": "Markdown",
            }).encode()
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=10)
        logger.info("✅ Telegram 推送成功")
    except Exception as e:
        logger.warning(f"Telegram 推送失败: {e}")


# ── 定时调度 ──────────────────────────────────────────

async def scheduler_loop():
    """
    每分钟检查是否到达报告时间。
    使用 Redis 记录最后运行日期，容器重启后不会重复执行。
    """
    logger.info(f"监控调度器启动，每天 {REPORT_HOUR:02d}:00 CST 生成简报")

    while True:
        await asyncio.sleep(60)
        try:
            now   = datetime.now()
            today = now.strftime("%Y-%m-%d")

            if now.hour < REPORT_HOUR:
                continue

            # 检查今天是否已经运行过
            r         = await get_redis()
            last_date = await r.get(LAST_RUN_KEY)
            if last_date == today:
                continue

            # 到点且今天还没运行
            logger.info(f"触发每日简报生成 ({today} {now.hour:02d}:{now.minute:02d})")
            await generate_and_push(REPORT_DAYS)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"scheduler_loop error: {e}", exc_info=True)


# ── FastAPI ───────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(scheduler_loop())
    logger.info("Monitoring service ready")
    yield
    task.cancel()


app = FastAPI(title="CryptoWatch v4 Monitoring Service", lifespan=lifespan)


@app.get("/health")
async def health():
    r = await get_redis()
    last = await r.get(LAST_RUN_KEY) or "从未运行"
    trend = await load_trend()
    return {
        "status":       "ok",
        "service":      "monitoring",
        "redis":        await redis_health(),
        "last_report":  last,
        "trend_days":   len(trend),
        "report_hour":  REPORT_HOUR,
        "push_enabled": PUSH_ENABLED,
        "exec_mode":    EXEC_MODE,
        "ts":           datetime.now().isoformat(),
    }


@app.post("/report/now")
async def trigger_now(days: int = Query(1, description="统计天数")):
    """手动立即触发一次报告生成"""
    logger.info(f"手动触发报告生成 days={days}")
    result = await generate_and_push(days)
    return {
        "status":     "ok",
        "generated":  datetime.now().isoformat(),
        "recommendation": result["report"].get("recommendation", {}),
        "trend":      result["trend"],
    }


@app.get("/report/latest")
async def latest_report():
    """获取最新一次报告的完整数据（JSON）"""
    r   = await get_redis()
    raw = await r.get("cw4:monitor:latest_report")
    if not raw:
        return {"status": "no_data", "msg": "尚未生成任何报告，POST /report/now 立即生成"}
    return json.loads(raw)


@app.get("/report/latest/text", response_class=PlainTextResponse)
async def latest_report_text():
    """获取最新一次报告的文本版本"""
    r   = await get_redis()
    raw = await r.get("cw4:monitor:latest_report")
    if not raw:
        return "尚未生成任何报告，请 POST /report/now 立即生成"
    data = json.loads(raw)
    import services.monitoring.report as rpt
    report_data = data.get("data", {})
    days        = data.get("days", 1)
    text        = rpt.render_report(report_data, days)
    trend_raw   = await get_redis()
    trend       = await load_trend()
    analysis    = analyze_trend(trend)
    return text + _build_trend_section(trend, analysis)


@app.get("/trend")
async def trend():
    """获取 IC 历史趋势数据"""
    t = await load_trend()
    return {
        "trend":    t[-30:],   # 最近30天
        "analysis": analyze_trend(t),
        "total_days": len(t),
    }


@app.get("/recommendation")
async def recommendation():
    """只返回切换建议，供 gateway dashboard 展示"""
    r   = await get_redis()
    raw = await r.get("cw4:monitor:latest_report")
    if not raw:
        return {"status": "no_data"}
    data = json.loads(raw)
    return {
        "recommendation": data.get("data", {}).get("recommendation", {}),
        "trend":          data.get("trend", {}),
        "generated_at":   data.get("generated_at"),
        "exec_mode":      EXEC_MODE,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("SERVICE_PORT", 8021)))
