"""
K1 — Phased dynamic Kelly base_size  (v2.1 §7.1)

Strictly phased: Phase 0 and Phase 1 use fixed base_size.
Phase 2+ use fractional Kelly (paper Month 3+ decision, Wiki-gated).

Kelly formula (trade-history version, §7.1):
  f* = (W·R - (1-W)) / R
  where W = win rate, R = avg_win / avg_loss

Phase schedule:
  Phase 0: paper Month 0-1  — fixed base_size, no Kelly
  Phase 1: paper Month 1-3  — fixed base_size, compute W/R diagnostics only
  Phase 2: paper Month 3-6  — quarter Kelly, cap [5%, 25%]
  Phase 3: paper Month 6+   — quarter Kelly, cap [3%, 30%]

Strategy 1: rolling 60-day window, min 30 trades before phase switch
Strategy 2: rolling 30-day window, min 30 trades before phase switch
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

import numpy as np


class KellyPhase(Enum):
    PHASE_0 = 0   # fixed size, no Kelly
    PHASE_1 = 1   # fixed size, diagnostic W/R only
    PHASE_2 = 2   # quarter Kelly, cap [5%, 25%]
    PHASE_3 = 3   # quarter Kelly, cap [3%, 30%]


# §7.1: fixed base_size fractions (Phase 0 and 1)
_FIXED_BASE_SIZE: dict[str, float] = {
    "strategy_1": 0.20,
    "strategy_2": 0.10,
}

# §7.1: rolling window lengths
_ROLLING_WINDOW_DAYS: dict[str, int] = {
    "strategy_1": 60,
    "strategy_2": 30,
}

# §7.1: Phase 2/3 caps
_PHASE2_CAP = (0.05, 0.25)
_PHASE3_CAP = (0.03, 0.30)

_MIN_TRADES_FOR_SWITCH = 30   # must have ≥ 30 closed trades to enable Phase 2
_QUARTER_KELLY = 0.25


@dataclass
class ClosedTrade:
    closed_at: datetime
    pnl_pct: float   # realised PnL as fraction (positive = win)


@dataclass
class KellyDiagnostic:
    """Computed W/R statistics for the current rolling window."""
    phase: KellyPhase
    sample_size: int
    win_rate: Optional[float]            # W
    avg_win_pct: Optional[float]         # avg win magnitude
    avg_loss_pct: Optional[float]        # avg loss magnitude (positive number)
    reward_risk_ratio: Optional[float]   # R = avg_win / avg_loss
    kelly_fraction: Optional[float]      # f* (full Kelly)
    quarter_kelly: Optional[float]       # 0.25 × f*
    suggested_base_pct: float            # what this phase actually uses
    negative_edge: bool = False          # True if f* < 0 → pause strategy


@dataclass
class KellySizer:
    """
    Stateful Kelly sizer for one strategy.

    Call record_trade() after each closed position.
    Call compute() to get the current diagnostic and suggested base_size.
    The phase field is set externally (Wiki-gated per §7.1).
    """

    strategy: str                    # 'strategy_1' or 'strategy_2'
    phase: KellyPhase = KellyPhase.PHASE_0
    _trades: deque[ClosedTrade] = field(default_factory=deque)

    def record_trade(self, closed_at: datetime, pnl_pct: float) -> None:
        """Record a closed trade's realised PnL fraction."""
        self._trades.append(ClosedTrade(closed_at=closed_at, pnl_pct=pnl_pct))
        self._evict(closed_at)

    def compute(self, nav_usdt: float, as_of: Optional[datetime] = None) -> KellyDiagnostic:
        """
        Compute current sizing diagnostic.

        Returns the suggested base_size as a fraction of nav.
        In Phase 0/1 this is always the fixed value; in Phase 2/3 it is
        quarter-Kelly capped to the phase bounds.
        """
        if as_of is not None:
            self._evict(as_of)

        window = list(self._trades)
        fixed = _FIXED_BASE_SIZE[self.strategy]

        if not window:
            return KellyDiagnostic(
                phase=self.phase,
                sample_size=0,
                win_rate=None,
                avg_win_pct=None,
                avg_loss_pct=None,
                reward_risk_ratio=None,
                kelly_fraction=None,
                quarter_kelly=None,
                suggested_base_pct=fixed,
            )

        wins = [t.pnl_pct for t in window if t.pnl_pct > 0]
        losses = [abs(t.pnl_pct) for t in window if t.pnl_pct <= 0]
        n = len(window)
        W = len(wins) / n if n > 0 else None
        avg_win = float(np.mean(wins)) if wins else None
        avg_loss = float(np.mean(losses)) if losses else None

        R: Optional[float] = None
        f_star: Optional[float] = None
        negative_edge = False

        if W == 0.0:
            # No wins at all — definitively negative edge regardless of losses
            negative_edge = True
        elif W is not None and avg_win is not None:
            if avg_loss is None:
                # All wins, no losses — bet maximum by convention (f* = 1.0)
                f_star = 1.0
            elif avg_loss > 0:
                R = avg_win / avg_loss
                f_star = (W * R - (1 - W)) / R
                if f_star < 0:
                    negative_edge = True

        quarter_k = (f_star * _QUARTER_KELLY) if f_star is not None else None

        if self.phase in (KellyPhase.PHASE_0, KellyPhase.PHASE_1):
            suggested = fixed
        elif self.phase == KellyPhase.PHASE_2:
            suggested = self._apply_kelly(quarter_k, negative_edge, fixed, _PHASE2_CAP)
        else:
            suggested = self._apply_kelly(quarter_k, negative_edge, fixed, _PHASE3_CAP)

        return KellyDiagnostic(
            phase=self.phase,
            sample_size=n,
            win_rate=W,
            avg_win_pct=avg_win,
            avg_loss_pct=avg_loss,
            reward_risk_ratio=R,
            kelly_fraction=f_star,
            quarter_kelly=quarter_k,
            suggested_base_pct=suggested,
            negative_edge=negative_edge,
        )

    def is_ready_for_phase2(self) -> bool:
        """True if min-30-trade criterion is met for Phase 2 switch."""
        return len(self._trades) >= _MIN_TRADES_FOR_SWITCH

    # ── Internals ─────────────────────────────────────────────────────────────

    def _evict(self, as_of: datetime) -> None:
        window_days = _ROLLING_WINDOW_DAYS[self.strategy]
        cutoff_ts = as_of.timestamp() - window_days * 86400
        while self._trades and self._trades[0].closed_at.timestamp() < cutoff_ts:
            self._trades.popleft()

    @staticmethod
    def _apply_kelly(
        quarter_k: Optional[float],
        negative_edge: bool,
        fixed_fallback: float,
        cap: tuple[float, float],
    ) -> float:
        if negative_edge or quarter_k is None:
            return fixed_fallback
        lo, hi = cap
        return max(lo, min(hi, quarter_k))
