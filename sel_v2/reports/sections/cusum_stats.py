"""§26.2 Section 2: CUSUM 触発統計."""
from __future__ import annotations

from typing import Any


def build_cusum_stats(data: dict[str, Any]) -> str:
    """
    Generate CUSUM trigger statistics section.

    Expected data keys:
      cusum_mid_triggers: int
      cusum_mid_avg_peak: float | None
      cusum_mid_h_range: tuple[float, float] | None
      cusum_short_triggers: int
      cusum_short_type_a_pct: float | None
      cusum_short_type_b_pct: float | None
    """
    mid_triggers = data.get("cusum_mid_triggers", 0)
    mid_avg_peak = data.get("cusum_mid_avg_peak")
    mid_h_range = data.get("cusum_mid_h_range")
    short_triggers = data.get("cusum_short_triggers", 0)
    short_a_pct = data.get("cusum_short_type_a_pct")
    short_b_pct = data.get("cusum_short_type_b_pct")

    def _fmt(v, fmt=".3f"):
        return f"{v:{fmt}}" if v is not None else "N/A"

    lines = [
        "## CUSUM 触発統計\n",
        "### CUSUM-Mid（策略 1）",
        f"- 触発次数: {mid_triggers}",
        f"- 平均 peak C 値: {_fmt(mid_avg_peak)}",
    ]
    if mid_h_range:
        lines.append(f"- 閾値 h_t 範囲: [{_fmt(mid_h_range[0])}, {_fmt(mid_h_range[1])}]")
    else:
        lines.append("- 閾値 h_t 範囲: N/A")

    lines += [
        "\n### CUSUM-Short（策略 2）",
        f"- 触発次数: {short_triggers}",
        f"- 類型 A（反転）入場比例: {_fmt(short_a_pct, '.1f')}%",
        f"- 類型 B（突破）入場比例: {_fmt(short_b_pct, '.1f')}%",
    ]
    return "\n".join(lines) + "\n"
