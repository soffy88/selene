"""
IC-decay closed loop (Phase 6).

The signal service already tracks a rolling Information Coefficient (IC = Spearman rank
correlation between the composite score and the realized return) per symbol, but nothing
acted on it. This module turns that IC into a *sizing multiplier* so the system responds to
alpha decay automatically: as predictive power fades, position sizing is throttled; as it
recovers, sizing is restored.

Design notes:
  - Neutral (1.0) until `min_trades` realized outcomes exist — a few noisy samples must not
    gate trading.
  - Never returns 0. A hard cut would stop new trades, which stops new outcomes, which freezes
    the IC estimate forever — a feedback deadlock. Keeping a `min_scale` floor lets the tracker
    keep observing and recover.
"""

from __future__ import annotations

import os

IC_MIN_TRADES = int(os.getenv("IC_MIN_TRADES", "20"))  # outcomes before gating activates
IC_GOOD = float(os.getenv("IC_GOOD", "0.05"))  # IC at/above which sizing is unrestricted
IC_FLOOR = float(os.getenv("IC_FLOOR", "0.0"))  # IC at/below which sizing hits its floor
IC_MIN_SCALE = float(os.getenv("IC_MIN_SCALE", "0.25"))  # floor multiplier (kept > 0 deliberately)


def ic_health_scalar(
    ic,
    n,
    *,
    min_trades: int = IC_MIN_TRADES,
    good: float = IC_GOOD,
    floor: float = IC_FLOOR,
    min_scale: float = IC_MIN_SCALE,
) -> float:
    """Map rolling IC -> sizing multiplier in [min_scale, 1.0].

    `ic` may be None (insufficient data); `n` is the number of realized outcomes.
    """
    if ic is None or n < min_trades:
        return 1.0
    if ic >= good:
        return 1.0
    if ic <= floor:
        return min_scale
    frac = (ic - floor) / (good - floor)
    return round(min_scale + (1.0 - min_scale) * frac, 4)
