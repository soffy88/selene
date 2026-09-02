"""
Shared pure quant helpers usable by any service without creating cross-service imports.
"""

from __future__ import annotations

from typing import Optional

# Perpetual funding settles every 8h.
FUNDING_HOURS = 8.0


def funding_adjusted_cost(base_cost: float, funding_rate: Optional[float], hold_hours: float, side: str) -> float:
    """Fold expected perpetual funding into a cost term (e.g. the Kelly `cost`).

    Longs pay funding when the rate is positive; shorts receive it. Returns base_cost plus the
    expected funding drag over the holding horizon (clamped at >= 0 — favorable funding is not
    counted as negative cost, which would inflate Kelly).
    """
    if not funding_rate:
        return base_cost
    periods = max(0.0, hold_hours) / FUNDING_HOURS
    sign = 1.0 if side.upper() in ("LONG", "BUY") else -1.0
    drag = sign * funding_rate * periods
    return base_cost + max(0.0, drag)
