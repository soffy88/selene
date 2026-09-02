"""§26.2 Section 9: Observation-only ツール触発統計 (v2.1 追加)."""

from __future__ import annotations

from typing import Any

_TOOL_LABELS = {
    "B1": "Bayesian HMM 軟検証 (B1)",
    "B2": "HMM 境界仲裁 (B2)",
    "TDA2": "TDA + clustering (TDA2)",
    "I1": "Permutation Entropy (I1)",
    "T2": "TE 滚動監控 (T2)",
    "W2": "Wavelet 多分形 (W2)",
    "H3": "Hawkes Cascade 早期預警 (H3)",
}


def build_observation_tools(data: dict[str, Any]) -> str:
    """
    Generate observation-only tool trigger statistics section.

    Expected data keys:
      tool_triggers: dict[tool_id, int]   — trigger count per tool in the week
      tool_notes: dict[tool_id, str]      — optional per-tool notes
    """
    tool_triggers: dict[str, int] = data.get("tool_triggers", {})
    tool_notes: dict[str, str] = data.get("tool_notes", {})

    lines = [
        "## Observation-only ツール触発統計（v2.1）\n",
        "| ツール ID | ツール名 | 週間触発数 | 備考 |",
        "|---|---|---|---|",
    ]

    for tid, label in _TOOL_LABELS.items():
        cnt = tool_triggers.get(tid, 0)
        note = tool_notes.get(tid, "N/A" if cnt == 0 else "")
        lines.append(f"| {tid} | {label} | {cnt} | {note} |")

    lines.append("\n> **注記**: 現在のデータ状態では B2/T2/H3 はほぼ無信号 — これは予期される動作（STUB データ不足）。")
    return "\n".join(lines) + "\n"
