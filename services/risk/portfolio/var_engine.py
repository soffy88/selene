"""
CryptoWatch v4 — Risk Engine: Historical VaR + CVaR + Dynamic Drawdown Controller.
Uses Historical Simulation — no normality assumption (crypto = fat tails).
"""
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class VaRResult:
    var_95: float; var_99: float; expected_shortfall: float
    confidence: float; n_observations: int
    method: str = "historical_simulation"


def calc_historical_var(returns_usd: list[float], confidence: float = 0.95) -> Optional[VaRResult]:
    if len(returns_usd) < 30:
        return None
    s = sorted(returns_usd)
    n = len(s)
    c95 = int((1 - 0.95) * n); c99 = int((1 - 0.99) * n)
    var_95 = abs(s[max(0, c95)]); var_99 = abs(s[max(0, c99)])
    tail = s[:max(1, c95)]
    cvar = abs(sum(tail) / len(tail)) if tail else var_95
    return VaRResult(var_95=round(var_95, 2), var_99=round(var_99, 2),
                     expected_shortfall=round(cvar, 2), confidence=confidence, n_observations=n)


@dataclass
class DrawdownLevel:
    name: str; threshold_lo: float; threshold_hi: float
    max_position_pct: float; new_trades_allowed: bool; description: str


DRAWDOWN_LEVELS = [
    DrawdownLevel("GREEN",  0.00, 0.05, 1.00, True,  "Normal — full position sizing"),
    DrawdownLevel("YELLOW", 0.05, 0.10, 0.50, True,  "Warning — 50% position sizes"),
    DrawdownLevel("ORANGE", 0.10, 0.15, 0.25, True,  "Alert — 25% positions"),
    DrawdownLevel("RED",    0.15, 1.00, 0.00, False, "HALT — all trading stopped"),
]


class DrawdownController:
    def __init__(self):
        self._peak = 0.0; self._current = 0.0; self._current_dd = 0.0
        self._max_dd = 0.0; self._level = DRAWDOWN_LEVELS[0]; self._halted_at = None

    def update(self, equity: float) -> DrawdownLevel:
        self._current = equity
        if equity > self._peak: self._peak = equity
        if self._peak > 0:
            self._current_dd = (self._peak - equity) / self._peak
            self._max_dd = max(self._max_dd, self._current_dd)
        self._level = self._classify(self._current_dd)
        if self._level.name == "RED" and self._halted_at is None:
            self._halted_at = datetime.utcnow()
            logger.critical(f"DRAWDOWN HALT: {self._current_dd:.1%} (peak ${self._peak:,.0f})")
        return self._level

    def _classify(self, dd: float) -> DrawdownLevel:
        for lvl in reversed(DRAWDOWN_LEVELS):
            if dd >= lvl.threshold_lo: return lvl
        return DRAWDOWN_LEVELS[0]

    def manual_reset(self):
        logger.warning("Drawdown controller manually reset")
        self._halted_at = None
        if self._level.name == "RED": self._level = DRAWDOWN_LEVELS[0]

    @property
    def current_dd(self): return self._current_dd
    @property
    def max_dd(self): return self._max_dd
    @property
    def level(self): return self._level
    @property
    def position_scalar(self): return self._level.max_position_pct
    @property
    def is_halted(self): return not self._level.new_trades_allowed

    def get_status(self) -> dict:
        return {"level": self._level.name, "current_dd_pct": round(self._current_dd*100, 2),
                "max_dd_pct": round(self._max_dd*100, 2), "peak_equity": self._peak,
                "position_scalar": self._level.max_position_pct,
                "new_trades": self._level.new_trades_allowed,
                "halted_at": self._halted_at.isoformat() if self._halted_at else None,
                "description": self._level.description}
