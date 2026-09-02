"""
Observation tool base classes  (v2.1 §2.2).

Observation tools are LAYER 2: they read market data and write signals to
v2_inverse_vocab_events. They must NEVER be called from strategy entry/exit
code paths.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


@dataclass
class BarFeatures:
    """Normalised 4H bar features passed to every observation tool."""

    timestamp: datetime
    log_return: float  # log(close / prev_close)
    volume: float  # raw base-asset volume
    funding_rate: Optional[float] = None  # BTC-USDT-SWAP 8H funding rate
    open_interest: Optional[float] = None  # OI in USD
    liquidation_usd: Optional[float] = None  # liquidation volume in USD (STUB)
    # v2.2 lens batch (CHAN/ICT tools) — optional, None-safe for legacy callers
    high: Optional[float] = None
    low: Optional[float] = None
    close: Optional[float] = None
    state: Optional[str] = None  # current sel state label, if known


@dataclass
class ObservationResult:
    """Output of a single observation tool update."""

    tool_id: str  # e.g. "B1", "TDA2"
    timestamp: datetime
    signal: bool  # True → anomaly / boundary / warning detected
    value: float  # primary scalar metric
    threshold: float  # the threshold value that was compared
    label: str  # human-readable state label
    confidence: float  # 0.0–1.0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def should_persist(self) -> bool:
        """True when the result should be written to v2_inverse_vocab_events."""
        return self.signal


@dataclass
class ToolEvaluationMetrics:
    """v2.1 §15.3 evaluation metrics for tool_evaluation_results table."""

    tool_id: str
    evaluation_period_start: datetime
    evaluation_period_end: datetime
    lead_time_seconds: Optional[float]  # median seconds before strategy trigger
    false_positive_rate: Optional[float]  # FP / (FP + TN)
    correlation_with_others: Optional[float]  # Spearman ρ with nearest peer tool
    sample_size: int
    decision: str  # "upgrade" | "maintain" | "deprecate"
    notes: str = ""


class ObservationTool(ABC):
    """Abstract base for all observation-only tools."""

    tool_id: str  # set by subclass

    @abstractmethod
    def update(self, bar: BarFeatures) -> ObservationResult:
        """
        Ingest one 4H bar and return an observation result.

        Implementations must be stateful (rolling window) and deterministic
        given the same history. They must NOT write to the DB themselves —
        the runner handles persistence.
        """

    @abstractmethod
    def is_ready(self) -> bool:
        """True once the tool has seen enough bars for its minimum window."""

    def reset(self) -> None:
        """Reset internal state (used in testing). Default clears nothing extra."""
        return None
