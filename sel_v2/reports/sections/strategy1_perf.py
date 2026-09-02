"""§26.2 Section 4: 策略 1 表現."""

from __future__ import annotations

from typing import Any


def build_strategy1_perf(data: dict[str, Any]) -> str:
    """
    Generate Strategy 1 performance section.

    Expected data keys:
      entries: int, exits: int, avg_hold_hours: float|None
      time_stop_triggers: int
      total_pnl_usdt: float|None, win_rate_pct: float|None
      avg_win_usdt: float|None, avg_loss_usdt: float|None
      max_single_drawdown_usdt: float|None
      critical_reduce_count: int, cascade_exit_count: int
      stop_loss_count: int
    """

    def _fmt(v, fmt=".2f"):
        return f"{v:{fmt}}" if v is not None else "N/A"

    entries = data.get("entries", 0)
    exits = data.get("exits", 0)
    avg_hold = data.get("avg_hold_hours")
    time_stop = data.get("time_stop_triggers", 0)
    pnl = data.get("total_pnl_usdt")
    wr = data.get("win_rate_pct")
    avg_win = data.get("avg_win_usdt")
    avg_loss = data.get("avg_loss_usdt")
    max_dd = data.get("max_single_drawdown_usdt")
    critical_reduce = data.get("critical_reduce_count", 0)
    cascade_exit = data.get("cascade_exit_count", 0)
    stop_loss = data.get("stop_loss_count", 0)

    lines = [
        "## 策略 1 表現\n",
        "### 入場/出場",
        f"- 入場次数: {entries}",
        f"- 出場次数: {exits}",
        f"- 平均持倉: {_fmt(avg_hold)} 時間",
        f"- Time Stop 触発次数: {time_stop}",
        "\n### PnL",
        f"- 総 PnL（USDT）: {_fmt(pnl)}",
        f"- 勝率: {_fmt(wr, '.1f')}%",
        f"- 平均盈利/亏損: {_fmt(avg_win)} / {_fmt(avg_loss)}",
        f"- 最大単笔回撤: {_fmt(max_dd)}",
        "\n### 風控触発",
        f"- Critical 減倉: {critical_reduce} 次",
        f"- Cascade 清倉: {cascade_exit} 次",
        f"- 浮亏 -3% 止損: {stop_loss} 次",
    ]
    return "\n".join(lines) + "\n"
