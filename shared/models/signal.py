"""
CryptoWatch v4 — ScoredSignal Model
The key v4 upgrade: signals carry win_probability + confidence bands,
not just BUY/SELL. This enables Kelly sizing and regime-aware filtering.
"""
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class SignalType(str, Enum):
    LONG_SETUP    = "LONG_SETUP"
    SHORT_SETUP   = "SHORT_SETUP"
    BEAR_SQUEEZE  = "BEAR_SQUEEZE"
    TREND_CONFIRM = "TREND_CONFIRM"
    CROWD_LONG    = "CROWD_LONG"
    CROWD_SHORT   = "CROWD_SHORT"
    FR_ARB        = "FR_ARB"


class Direction(str, Enum):
    LONG    = "LONG"
    SHORT   = "SHORT"
    NEUTRAL = "NEUTRAL"   # used for FR_ARB (delta-neutral)


class Regime(str, Enum):
    TRENDING_UP    = "TRENDING_UP"
    TRENDING_DOWN  = "TRENDING_DOWN"
    RANGING        = "RANGING"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    ACCUMULATION   = "ACCUMULATION"
    UNKNOWN        = "UNKNOWN"


@dataclass
class ScoredSignal:
    """
    v4 signal output. Contains:
    - Probability layer: win_probability, confidence_band, expected_return
    - Regime context: which market state triggered this
    - Factor breakdown: which factors contributed (auditable)
    - Execution params: entry/stop/take computed at signal time
    """
    # Identity
    id:             str = field(default_factory=lambda: str(uuid.uuid4()))
    symbol:         str = ""
    signal_type:    SignalType = SignalType.LONG_SETUP
    direction:      Direction  = Direction.LONG
    regime:         Regime     = Regime.UNKNOWN

    # ── Probability layer (v4 core upgrade) ───────────────────────────────────
    win_probability:   float = 0.5        # 0.0–1.0, from WFO calibration
    confidence_lo:     float = 0.45       # 95% confidence interval lower
    confidence_hi:     float = 0.55       # 95% confidence interval upper
    expected_return:   float = 0.0        # E[R] = p*take_dist - (1-p)*stop_dist
    factor_scores:     dict  = field(default_factory=dict)  # factor → score
    regime_adjusted:   bool  = False      # whether regime modified the score

    # ── Execution parameters ───────────────────────────────────────────────────
    entry_price:    float = 0.0
    stop_loss:      float = 0.0
    take_profit:    float = 0.0
    max_hold_hours: int   = 24            # regime-specific holding limit

    # ── Data quality ───────────────────────────────────────────────────────────
    data_quality:   float = 1.0           # 0–1, reject if < MIN_DATA_QUALITY
    indicators:     dict  = field(default_factory=dict)  # raw indicator snapshot

    # ── Metadata ───────────────────────────────────────────────────────────────
    timestamp:      datetime = field(default_factory=datetime.utcnow)
    cooldown:       int      = 14400      # seconds, same symbol won't re-fire

    @property
    def is_actionable(self) -> bool:
        """Signal meets minimum quality bars for Portfolio consideration."""
        import os
        min_quality = float(os.getenv("MIN_DATA_QUALITY", "0.75"))
        return (
            self.win_probability >= 0.55 and
            self.data_quality >= min_quality and
            self.entry_price > 0 and
            self.stop_loss > 0
        )

    @property
    def risk_reward(self) -> float:
        """Actual R:R ratio from entry to stop/take."""
        stop_dist = abs(self.entry_price - self.stop_loss)
        take_dist = abs(self.entry_price - self.take_profit)
        return take_dist / stop_dist if stop_dist > 0 else 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "symbol": self.symbol,
            "signal_type": self.signal_type.value,
            "direction": self.direction.value,
            "regime": self.regime.value,
            "win_probability": self.win_probability,
            "confidence_lo": self.confidence_lo,
            "confidence_hi": self.confidence_hi,
            "expected_return": self.expected_return,
            "factor_scores": self.factor_scores,
            "regime_adjusted": self.regime_adjusted,
            "entry_price": self.entry_price,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "max_hold_hours": self.max_hold_hours,
            "risk_reward": self.risk_reward,
            "data_quality": self.data_quality,
            "indicators": self.indicators,
            "timestamp": self.timestamp.isoformat(),
            "cooldown": self.cooldown,
            "is_actionable": self.is_actionable,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ScoredSignal":
        s = cls()
        s.id = d.get("id", s.id)
        s.symbol = d.get("symbol", "")
        s.signal_type = SignalType(d.get("signal_type", "LONG_SETUP"))
        s.direction = Direction(d.get("direction", "LONG"))
        s.regime = Regime(d.get("regime", "UNKNOWN"))
        s.win_probability = d.get("win_probability", 0.5)
        s.confidence_lo = d.get("confidence_lo", 0.45)
        s.confidence_hi = d.get("confidence_hi", 0.55)
        s.expected_return = d.get("expected_return", 0.0)
        s.factor_scores = d.get("factor_scores", {})
        s.entry_price = d.get("entry_price", 0.0)
        s.stop_loss = d.get("stop_loss", 0.0)
        s.take_profit = d.get("take_profit", 0.0)
        s.data_quality = d.get("data_quality", 1.0)
        s.indicators = d.get("indicators", {})
        if "timestamp" in d:
            s.timestamp = datetime.fromisoformat(d["timestamp"])
        return s
