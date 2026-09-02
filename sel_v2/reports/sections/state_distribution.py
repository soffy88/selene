"""§26.2 Section 1: 状態機統計 — state distribution and transitions."""

from __future__ import annotations

from typing import Any


def build_state_distribution(data: dict[str, Any]) -> str:
    """
    Generate the state distribution section.

    Expected data keys:
      state_hours: dict[state_name, float]  — hours in each state
      state_switches: dict[state_name, int] — switch count into each state
      transitions: list[dict]               — transition event records
      total_hours: float                    — total hours in period
    """
    state_hours: dict[str, float] = data.get("state_hours", {})
    state_switches: dict[str, int] = data.get("state_switches", {})
    transitions: list[dict] = data.get("transitions", [])
    total_hours: float = data.get("total_hours", 168.0)  # 1 week = 168h

    states = [
        "Coiling",
        "Surging",
        "Drifting_Calm",
        "Drifting_Charged",
        "Critical",
        "Cascade",
    ]

    lines = ["## 状態機統計\n", "### 4H 状態分布（本週）\n"]
    lines.append("| 状態 | 時長 (h) | 占比 (%) | 切換次数 |")
    lines.append("|---|---|---|---|")
    for s in states:
        h = state_hours.get(s, 0.0)
        pct = (h / total_hours * 100) if total_hours > 0 else 0.0
        sw = state_switches.get(s, 0)
        lines.append(f"| {s} | {h:.1f} | {pct:.1f} | {sw} |")

    lines.append("\n### 状態転換事件")
    if transitions:
        for i, t in enumerate(transitions, 1):
            ts = t.get("timestamp", "N/A")
            frm = t.get("from_state", "?")
            to = t.get("to_state", "?")
            cond = t.get("trigger_condition", "")
            lines.append(f"- 転換 {i}: {frm} → {to}, {ts}, {cond}")
    else:
        lines.append("- N/A（本週無転換記録）")

    return "\n".join(lines) + "\n"
