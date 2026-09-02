"""§26.2 Section 8: 週末口座状態."""

from __future__ import annotations

from typing import Any


def build_account_state(data: dict[str, Any]) -> str:
    """
    Generate end-of-week account state section.

    Expected data keys:
      sub_account_1_usdt: float | None
      sub_account_2_usdt: float | None
      total_pct_change: float | None
    """

    def _fmt(v, fmt=".2f"):
        return f"{v:{fmt}}" if v is not None else "N/A"

    sa1 = data.get("sub_account_1_usdt")
    sa2 = data.get("sub_account_2_usdt")
    pct_change = data.get("total_pct_change")

    sign = "+" if (pct_change is not None and pct_change >= 0) else ""
    pct_str = f"{sign}{_fmt(pct_change, '.1f')}%" if pct_change is not None else "N/A"

    lines = [
        "## 週末口座状態\n",
        f"- サブ口座 1 資金: {_fmt(sa1)} USDT",
        f"- サブ口座 2 資金: {_fmt(sa2)} USDT",
        f"- 総資金 vs 先週: {pct_str}",
    ]
    return "\n".join(lines) + "\n"
