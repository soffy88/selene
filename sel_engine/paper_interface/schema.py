"""Output schemas for the sel paper trading interface (Wave 4)."""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sel_engine.states.schema import StateLabel


@dataclass
class StateOutput:
    """One complete state record emitted per 1H bar. Paper trading module reads this."""

    bar_time: datetime  # UTC 1H bar start time
    symbol: str
    state: Optional[str]  # e.g. "Coiling", None during cold start
    state_enum: Optional[StateLabel]
    direction: Optional[str]  # "Up" / "Down" / None (only for Surging states)
    is_cold_start: bool
    confidence: float  # 1.0 for now (placeholder for future calibration)
    reason: str  # e.g. "COILING:sigma_p@0.15+autocorr_24h@0.78+OI@0.62"
    is_legal_transition: bool
    transition_from: Optional[str]  # previous state string
    health_warning: bool  # True if any health metric out of range this bar
    # Feature snapshot (for decision_trail)
    close: Optional[float]
    sigma_p_24h: Optional[float]
    H: Optional[float]
    TF: Optional[float]
    OI: Optional[float]
    funding_rate: Optional[float]
    LV: Optional[float]
    feature_completeness: float  # fraction of non-None features
    none_reason: Optional[str] = None  # StateNoneReason.value when state is None, else None


@dataclass
class StateChangeEvent:
    """Emitted to Redis Stream when state changes."""

    event_time: datetime
    symbol: str
    previous_state: Optional[str]
    new_state: Optional[str]
    bar_time: datetime
    reason: str
