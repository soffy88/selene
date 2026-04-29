"""§26.2 Section 7: 異常事件日誌."""
from __future__ import annotations

from typing import Any


def build_anomalies(data: dict[str, Any]) -> str:
    """
    Generate anomaly event log section.

    Expected data keys:
      data_gaps: list[str]
      api_errors: list[str]
      decision_anomalies: list[str]
    """
    data_gaps: list[str] = data.get("data_gaps", [])
    api_errors: list[str] = data.get("api_errors", [])
    decision_anomalies: list[str] = data.get("decision_anomalies", [])

    lines = ["## 異常事件日誌\n"]

    def _section(title: str, items: list[str]) -> list[str]:
        out = [f"- {title}: "]
        if items:
            for item in items:
                out.append(f"  - {item}")
        else:
            out[-1] += "なし"
        return out

    lines += _section("データ欠失/遅延", data_gaps)
    lines += _section("API エラー", api_errors)
    lines += _section("決策異常", decision_anomalies)

    return "\n".join(lines) + "\n"
