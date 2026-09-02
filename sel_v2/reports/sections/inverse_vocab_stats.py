"""§26.2 Section 3: 反推詞彙出現統計."""

from __future__ import annotations

from typing import Any


def build_inverse_vocab_stats(data: dict[str, Any]) -> str:
    """
    Generate inverse vocabulary occurrence statistics.

    Expected data keys:
      vocab_counts: dict[vocab_name, int]  — occurrence count per vocab
    """
    vocab_counts: dict[str, int] = data.get("vocab_counts", {})

    known_vocabs = [
        "Sweep",
        "Absorption",
        "Imprinting",
        "Saturation",
        "Exhaustion",
        "Crowding",
        "Release",
    ]

    lines = [
        "## 反推詞彙出現統計\n",
        "| 詞彙 | 出現次数 | 入場決策中の役割 |",
        "|---|---|---|",
    ]
    role_map = {
        "Sweep": "策略 2 類型 A 増強 / 策略 1 増強",
        "Absorption": "策略 2 類型 A 反転入場 / 策略 1 置信度",
        "Imprinting": "observation-only",
        "Saturation": "策略 1 Coiling 入場増強",
        "Exhaustion": "策略 1 Surging 出場",
        "Crowding": "策略 1 同向仓位減半 / 策略 2 反転増強",
        "Release": "策略 1 入場主シグナル",
    }
    for v in known_vocabs:
        cnt = vocab_counts.get(v, 0)
        role = role_map.get(v, "N/A")
        lines.append(f"| {v} | {cnt} | {role} |")

    # Any extra vocabs not in the standard set
    extras = {k: v for k, v in vocab_counts.items() if k not in known_vocabs}
    for v, cnt in sorted(extras.items()):
        lines.append(f"| {v} | {cnt} | - |")

    return "\n".join(lines) + "\n"
