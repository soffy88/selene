"""§26.2 Section 5: 策略 2 表現."""
from __future__ import annotations

from typing import Any


def build_strategy2_perf(data: dict[str, Any]) -> str:
    """
    Generate Strategy 2 performance section.

    Expected data keys:
      entries: int, exits: int
      type_a_pct: float|None, type_b_pct: float|None
      time_stop_24h: int
      total_pnl_usdt: float|None, win_rate_pct: float|None
      type_a_win_rate_pct: float|None, type_b_win_rate_pct: float|None
      avg_win_usdt: float|None, avg_loss_usdt: float|None
      cusum_peak_sl_count: int, stop_loss_count: int
      cascade_exit_count: int
    """

    def _fmt(v, fmt=".2f"):
        return f"{v:{fmt}}" if v is not None else "N/A"

    entries = data.get("entries", 0)
    exits = data.get("exits", 0)
    type_a_pct = data.get("type_a_pct")
    type_b_pct = data.get("type_b_pct")
    time_stop = data.get("time_stop_24h", 0)
    pnl = data.get("total_pnl_usdt")
    wr = data.get("win_rate_pct")
    a_wr = data.get("type_a_win_rate_pct")
    b_wr = data.get("type_b_win_rate_pct")
    avg_win = data.get("avg_win_usdt")
    avg_loss = data.get("avg_loss_usdt")
    cusum_sl = data.get("cusum_peak_sl_count", 0)
    stop_loss = data.get("stop_loss_count", 0)
    cascade_exit = data.get("cascade_exit_count", 0)

    lines = [
        "## 策略 2 表現\n",
        "### 入場/出場",
        f"- 入場次数: {entries}",
        f"- 出場次数: {exits}",
        f"- 類型 A 入場比例: {_fmt(type_a_pct, '.1f')}%",
        f"- 類型 B 入場比例: {_fmt(type_b_pct, '.1f')}%",
        f"- Time Stop 24h 触発次数: {time_stop}",
        "\n### PnL",
        f"- 総 PnL（USDT）: {_fmt(pnl)}",
        f"- 勝率: {_fmt(wr, '.1f')}%",
        f"- 類型 A 勝率: {_fmt(a_wr, '.1f')}%",
        f"- 類型 B 勝率: {_fmt(b_wr, '.1f')}%",
        f"- 平均盈利/亏損: {_fmt(avg_win)} / {_fmt(avg_loss)}",
        "\n### 風控触発",
        f"- CUSUM 創新高 SL: {cusum_sl} 次",
        f"- 浮亏 -2% 止損: {stop_loss} 次",
        f"- Cascade 清倉: {cascade_exit} 次",
    ]
    return "\n".join(lines) + "\n"
