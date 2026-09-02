"""§26.2 Section 6: 協調層事件."""

from __future__ import annotations

from typing import Any


def build_coordination_events(data: dict[str, Any]) -> str:
    """
    Generate coordination layer events section.

    Expected data keys:
      reverse_hedge_count: int
      independent_same_direction_count: int
    """
    reverse = data.get("reverse_hedge_count", 0)
    same_dir = data.get("independent_same_direction_count", 0)

    lines = [
        "## 協調層事件\n",
        f"- 反向対冲発生次数: {reverse}",
        f"- 同向独立運行次数: {same_dir}",
    ]
    return "\n".join(lines) + "\n"
